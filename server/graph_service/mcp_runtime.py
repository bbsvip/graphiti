"""Mount the existing Graphiti MCP tools using the same runtime as /admin and REST."""

import sys
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from graphiti_core import Graphiti
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from graph_service.config import Settings
from graph_service.runtime_clients import require_connections

# The upstream MCP service uses top-level imports from its src directory. Docker
# copies that source next to graph_service; local runs use the repository checkout.
MCP_SOURCE = Path(__file__).resolve().parents[1] / 'mcp_server' / 'src'
if not (MCP_SOURCE / 'graphiti_mcp_server.py').is_file():
    MCP_SOURCE = Path(__file__).resolve().parents[2] / 'mcp_server' / 'src'
if str(MCP_SOURCE) not in sys.path:
    sys.path.insert(0, str(MCP_SOURCE))
upstream: Any = import_module('graphiti_mcp_server')


class RuntimeGraphitiService(upstream.GraphitiService):
    async def get_client(self) -> Graphiti:
        require_connections()
        if self.client is None:
            raise RuntimeError('MCP runtime is not initialized')
        return self.client


class RuntimeQueueService(upstream.QueueService):
    async def add_episode(self, **kwargs: Any) -> int:
        # Do not accept background work that will fail only after acknowledging it.
        require_connections()
        return await super().add_episode(**kwargs)


def create_mcp_app(settings: Settings) -> Starlette:
    hosts = ['127.0.0.1:*', 'localhost:*', '[::1]:*']
    origins = ['http://127.0.0.1:*', 'http://localhost:*', 'http://[::1]:*']
    if settings.mcp_public_url:
        url = urlsplit(str(settings.mcp_public_url))
        hosts.append(url.netloc)
        origins.append(f'{url.scheme}://{url.netloc}')
    return upstream.mcp.streamable_http_app(
        streamable_http_path='/',
        json_response=True,
        # MCP clients need a session ID from initialize for subsequent tool calls.
        stateless_http=False,
        transport_security=TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins),
    )


@asynccontextmanager
async def mcp_lifespan(settings: Settings, client: Graphiti):
    config = upstream.GraphitiConfig(
        database={'provider': settings.db_backend},
        graphiti={'group_id': settings.mcp_group_id},
    )
    service = RuntimeGraphitiService(config, upstream.SEMAPHORE_LIMIT)
    service.client = client
    service.entity_types = upstream.build_entity_types(config.graphiti.entity_types)
    service.edge_types = upstream.build_edge_types(config.graphiti.edge_types)
    service.edge_type_map = upstream.build_edge_type_map(config.graphiti.edge_type_map)
    queue = RuntimeQueueService()
    await queue.initialize(client)
    upstream.config = config
    upstream.graphiti_service = service
    upstream.queue_service = queue
    upstream.graphiti_client = client
    upstream.semaphore = service.semaphore
    try:
        async with upstream.mcp.session_manager.run():
            yield
    finally:
        await queue.shutdown()
        upstream.graphiti_service = None
        upstream.queue_service = None
        upstream.graphiti_client = None
        upstream._group_drivers.clear()
