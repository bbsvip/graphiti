import logging
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException
from graphiti_core import Graphiti  # type: ignore
from graphiti_core.edges import EntityEdge  # type: ignore
from graphiti_core.errors import EdgeNotFoundError, GroupsEdgesNotFoundError, NodeNotFoundError
from graphiti_core.llm_client import LLMClient  # type: ignore
from graphiti_core.nodes import EntityNode, EpisodicNode  # type: ignore

from graph_service.compatible_client import CompatibleLLMClient
from graph_service.config import ZepEnvDep
from graph_service.dto import FactResult
from graph_service.local_clients import InfinityEmbedder, InfinityReranker
from graph_service.oauth_client import OpenAIOAuthClient
from graph_service.openai_auth import connection_settings, get_openai_auth

logger = logging.getLogger(__name__)


class ZepGraphiti(Graphiti):
    def __init__(
        self,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
        llm_client: LLMClient | None = None,
        **kwargs,
    ):
        super().__init__(uri, user, password, llm_client, **kwargs)  # type: ignore

    async def save_entity_node(self, name: str, uuid: str, group_id: str, summary: str = ''):
        new_node = EntityNode(
            name=name,
            uuid=uuid,
            group_id=group_id,
            summary=summary,
        )
        await new_node.generate_name_embedding(self.embedder)
        await new_node.save(self.driver)
        return new_node

    async def get_entity_edge(self, uuid: str):
        try:
            edge = await EntityEdge.get_by_uuid(self.driver, uuid)
            return edge
        except EdgeNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message) from e

    async def delete_group(self, group_id: str):
        try:
            edges = await EntityEdge.get_by_group_ids(self.driver, [group_id])
        except GroupsEdgesNotFoundError:
            logger.warning(f'No edges found for group {group_id}')
            edges = []

        nodes = await EntityNode.get_by_group_ids(self.driver, [group_id])

        episodes = await EpisodicNode.get_by_group_ids(self.driver, [group_id])

        for edge in edges:
            await edge.delete(self.driver)

        for node in nodes:
            await node.delete(self.driver)

        for episode in episodes:
            await episode.delete(self.driver)

    async def delete_entity_edge(self, uuid: str):
        try:
            edge = await EntityEdge.get_by_uuid(self.driver, uuid)
            await edge.delete(self.driver)
        except EdgeNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message) from e

    async def delete_episodic_node(self, uuid: str):
        try:
            episode = await EpisodicNode.get_by_uuid(self.driver, uuid)
            await episode.delete(self.driver)
        except NodeNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message) from e


def _create_graphiti_client(settings: ZepEnvDep, **provider_clients) -> ZepGraphiti:
    """Create a ZepGraphiti client based on the configured database backend."""
    if settings.db_backend == 'falkordb':
        from graphiti_core.driver.falkordb_driver import FalkorDriver

        driver = FalkorDriver(  # type: ignore
            host=settings.falkordb_host or 'localhost',  # type: ignore
            port=settings.falkordb_port or 6379,  # type: ignore
            database=settings.falkordb_database or 'default_db',  # type: ignore
        )
        return ZepGraphiti(graph_driver=driver, **provider_clients)  # type: ignore
    else:
        # Validate Neo4j settings are present
        if not all([settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password]):
            raise ValueError(
                'Neo4j configuration (neo4j_uri, neo4j_user, neo4j_password) is required '
                "when db_backend is 'neo4j'"
            )
        return ZepGraphiti(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
            **provider_clients,
        )


@asynccontextmanager
async def configured_graphiti(settings: ZepEnvDep):
    from graphiti_core.embedder.client import EMBEDDING_DIM

    auth = get_openai_auth()
    config = connection_settings()
    async with httpx.AsyncClient(timeout=120) as http:
        embedder = InfinityEmbedder(
            http, config['local_model_url'], config['embedding_model'], EMBEDDING_DIM, auth.store
        )
        reranker = InfinityReranker(
            http, config['local_model_url'], config['reranker_model'], auth.store
        )
        llm_client = (
            CompatibleLLMClient(
                http, config['llm_base_url'], config['model'], config['small_model'], auth.store
            )
            if config.get('llm_provider', 'oauth') == 'custom'
            else OpenAIOAuthClient(auth)
        )
        client = _create_graphiti_client(
            settings, llm_client=llm_client, embedder=embedder, cross_encoder=reranker
        )
        try:
            yield client
        finally:
            await client.close()


async def get_graphiti(settings: ZepEnvDep):
    account = get_openai_auth().active_account()
    config = connection_settings()
    if config.get('llm_provider', 'oauth') == 'oauth' and (
        not account or not account.get('access_token')
    ):
        raise HTTPException(503, 'Sign in to OpenAI at /admin first')
    if config.get('llm_provider') == 'custom' and not config.get('llm_base_url'):
        raise HTTPException(503, 'Configure the custom LLM base URL at /admin first')
    if not all(config.get(key) for key in ('model', 'small_model', 'reranker_model')):
        raise HTTPException(503, 'Choose the LLM and Infinity models at /admin first')
    async with configured_graphiti(settings) as client:
        yield client


async def initialize_graphiti(settings: ZepEnvDep):
    async with configured_graphiti(settings) as client:
        await client.build_indices_and_constraints()


def get_fact_result_from_edge(edge: EntityEdge):
    return FactResult(
        uuid=edge.uuid,
        name=edge.name,
        fact=edge.fact,
        valid_at=edge.valid_at,
        invalid_at=edge.invalid_at,
        created_at=edge.created_at,
        expired_at=edge.expired_at,
        source_node_uuid=edge.source_node_uuid,
        target_node_uuid=edge.target_node_uuid,
        episodes=edge.episodes or [],
    )


ZepGraphitiDep = Annotated[ZepGraphiti, Depends(get_graphiti)]
