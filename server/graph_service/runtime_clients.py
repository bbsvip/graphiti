"""Providers for the persistent MCP client, resolved from the shared admin runtime."""

from collections.abc import Iterable
from typing import Any

import httpx
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.client import EMBEDDING_DIM, EmbedderClient, EmbedderConfig
from graphiti_core.llm_client import LLMClient, LLMConfig
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, ModelSize
from graphiti_core.prompts import Message
from pydantic import BaseModel

from graph_service import openai_auth
from graph_service.compatible_client import CompatibleLLMClient
from graph_service.local_clients import InfinityEmbedder, InfinityReranker
from graph_service.oauth_client import OpenAIOAuthClient


def require_connections() -> dict[str, Any]:
    config = openai_auth.connection_settings()
    if config.get('llm_provider', 'oauth') == 'oauth':
        account = openai_auth.get_openai_auth().active_account()
        if not account or not account.get('access_token'):
            raise ValueError('Sign in to OpenAI at /admin first')
    elif not config.get('llm_base_url'):
        raise ValueError('Configure the custom LLM base URL at /admin first')
    if not all(config.get(key) for key in ('model', 'small_model', 'reranker_model')):
        raise ValueError('Choose the LLM and Infinity models at /admin first')
    return config


class RuntimeLLMClient(LLMClient):
    def __init__(self, http: httpx.AsyncClient):
        super().__init__(LLMConfig(), cache=False)
        self.http = http

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, Any]:
        config = require_connections()
        auth = openai_auth.get_openai_auth()
        client = (
            CompatibleLLMClient(
                self.http,
                config['llm_base_url'],
                config['model'],
                config['small_model'],
                auth.store,
            )
            if config.get('llm_provider', 'oauth') == 'custom'
            else OpenAIOAuthClient(auth)
        )
        client.token_tracker = self.token_tracker
        client.set_tracer(self.tracer)
        return await client._generate_response(messages, response_model, max_tokens, model_size)


class RuntimeEmbedder(EmbedderClient):
    def __init__(self, http: httpx.AsyncClient, dimensions: int = EMBEDDING_DIM):
        self.http = http
        self.config = EmbedderConfig(embedding_dim=dimensions)

    def _client(self) -> InfinityEmbedder:
        config = openai_auth.connection_settings()
        return InfinityEmbedder(
            self.http,
            config['local_model_url'],
            config['embedding_model'],
            self.config.embedding_dim,
            openai_auth.get_openai_auth().store,
        )

    async def create(
        self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]
    ) -> list[float]:
        return await self._client().create(input_data)

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        return await self._client().create_batch(input_data_list)


class RuntimeReranker(CrossEncoderClient):
    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        config = openai_auth.connection_settings()
        client = InfinityReranker(
            self.http,
            config['local_model_url'],
            config['reranker_model'],
            openai_auth.get_openai_auth().store,
        )
        return await client.rank(query, passages)
