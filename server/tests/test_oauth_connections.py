import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from graph_service.openai_auth import OpenAIAuth, RuntimeStore


def test_registration_uses_own_client_and_pkce(tmp_path):
    auth = OpenAIAuth(RuntimeStore(tmp_path), callback_port=8000)
    attempt = auth.begin_login()
    query = parse_qs(urlsplit(attempt['url']).query)
    assert query['client_id'] == ['dynamic_agent_client']
    assert query['redirect_uri'] == ['http://127.0.0.1:8000/auth/callback']
    assert query['resource'] == ['https://api.openai.com/v1']
    assert query['code_challenge_method'] == ['S256']
    assert 'chatgpt.tokens.use.direct' in query['scope'][0]
    assert OpenAIAuth(RuntimeStore(tmp_path), 8000).store.get('host_id') == auth.store.get(
        'host_id'
    )


@pytest.mark.asyncio
async def test_wrong_state_never_exchanges_code(tmp_path):
    auth = OpenAIAuth(RuntimeStore(tmp_path), 8000)
    auth.begin_login()
    with pytest.raises(ValueError, match='state'):
        await auth.finish_login({'state': 'attacker', 'code': 'secret'})


def test_usage_survives_restart_and_does_not_count_cached_tokens_twice(tmp_path):
    store = RuntimeStore(tmp_path)
    store.record_usage('llm', 'm', 30, 10, cached_tokens=20)
    restarted = RuntimeStore(tmp_path)
    summary = restarted.usage_summary()
    assert summary['totals']['input_tokens'] == 30
    assert summary['totals']['output_tokens'] == 10
    assert summary['totals']['cached_tokens'] == 20
    assert summary['totals']['total_tokens'] == 40


@pytest.mark.asyncio
async def test_infinity_embeddings_keep_input_order_and_duplicates(tmp_path):
    from graph_service.local_clients import InfinityEmbedder

    def handle(request):
        assert request.url.path == '/embeddings'
        assert 'authorization' not in request.headers
        assert json.loads(request.content)['input'] == ['same', 'same']
        return httpx.Response(
            200,
            json={
                'data': [
                    {'index': 1, 'embedding': [0.0, 1.0]},
                    {'index': 0, 'embedding': [1.0, 0.0]},
                ],
                'usage': {'prompt_tokens': 4},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        embedder = InfinityEmbedder(http, 'http://local/', 'BAAI/bge-m3', 2, RuntimeStore(tmp_path))
        assert await embedder.create_batch(['same', 'same']) == [[1.0, 0.0], [0.0, 1.0]]


@pytest.mark.asyncio
async def test_bad_embedding_dimension_fails_before_graph_write(tmp_path):
    from graph_service.local_clients import InfinityEmbedder

    def handle(_):
        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1.0]}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        embedder = InfinityEmbedder(http, 'http://local/', 'm', 2, RuntimeStore(tmp_path))
        with pytest.raises(ValueError, match='dimension'):
            await embedder.create('hello')


@pytest.mark.asyncio
async def test_rest_startup_wires_keyless_clients_without_contacting_model_services(
    tmp_path, monkeypatch
):
    from graph_service import zep_graphiti
    from graph_service.config import Settings
    from graph_service.local_clients import InfinityEmbedder, InfinityReranker
    from graph_service.oauth_client import OpenAIOAuthClient

    auth = OpenAIAuth(RuntimeStore(tmp_path), 8000)
    settings = Settings(
        _env_file=None, neo4j_uri='bolt://unused:7687', neo4j_user='neo4j', neo4j_password='test'
    )
    monkeypatch.setattr(zep_graphiti, 'get_openai_auth', lambda: auth)
    monkeypatch.setattr(
        zep_graphiti,
        'connection_settings',
        lambda: {
            'local_model_url': 'http://unused',
            'embedding_model': 'embedding',
            'reranker_model': 'reranker',
        },
    )
    calls = []

    class Graph:
        def __init__(self, **kwargs):
            assert isinstance(kwargs['llm_client'], OpenAIOAuthClient)
            assert isinstance(kwargs['embedder'], InfinityEmbedder)
            assert isinstance(kwargs['cross_encoder'], InfinityReranker)
            assert kwargs['embedder'].http is kwargs['cross_encoder'].http
            self.http = kwargs['embedder'].http
            calls.append(self)

        async def build_indices_and_constraints(self):
            calls.append('indices')

        async def close(self):
            calls.append('closed')

    monkeypatch.setattr(zep_graphiti, 'ZepGraphiti', Graph)
    await zep_graphiti.initialize_graphiti(settings)
    assert calls[1:] == ['indices', 'closed']
    assert calls[0].http.is_closed


@pytest.mark.asyncio
async def test_unconfigured_requests_fail_before_ingest_is_accepted(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from graph_service import zep_graphiti
    from graph_service.config import Settings

    auth = OpenAIAuth(RuntimeStore(tmp_path), 8000)
    monkeypatch.setattr(zep_graphiti, 'get_openai_auth', lambda: auth)
    monkeypatch.setattr(zep_graphiti, 'connection_settings', lambda: {})
    dependency = zep_graphiti.get_graphiti(Settings(_env_file=None))
    with pytest.raises(HTTPException) as error:
        await anext(dependency)
    assert error.value.status_code == 503
