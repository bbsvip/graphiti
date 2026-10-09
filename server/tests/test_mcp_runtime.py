import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from graphiti_core.prompts import Message
from pydantic import BaseModel

from graph_service.openai_auth import OpenAIAuth, RuntimeStore


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    auth = OpenAIAuth(RuntimeStore(tmp_path), 8123)
    config = {
        'llm_provider': 'custom',
        'llm_base_url': 'http://llm/v1',
        'model': 'first',
        'small_model': 'small',
        'local_model_url': 'http://infinity',
        'embedding_model': 'embed-one',
        'reranker_model': 'rank-one',
    }
    monkeypatch.setattr('graph_service.openai_auth.get_openai_auth', lambda: auth)
    monkeypatch.setattr('graph_service.openai_auth.connection_settings', lambda: config.copy())
    return auth, config


class Result(BaseModel):
    answer: str


@pytest.mark.asyncio
async def test_persistent_clients_follow_saved_settings_and_share_usage(runtime, monkeypatch):
    from graph_service.runtime_clients import RuntimeEmbedder, RuntimeLLMClient, RuntimeReranker

    auth, config = runtime
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append((str(request.url), body['model']))
        assert 'authorization' not in request.headers
        if request.url.path == '/embeddings':
            return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1.0, 2.0]}]})
        if request.url.path == '/rerank':
            return httpx.Response(200, json={'results': [{'index': 0, 'relevance_score': 0.8}]})
        return httpx.Response(
            200,
            json={
                'choices': [{'message': {'content': '{"answer":"ok"}'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 3, 'completion_tokens': 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        llm = RuntimeLLMClient(http)
        embedder = RuntimeEmbedder(http, dimensions=2)
        reranker = RuntimeReranker(http)
        for iteration in range(2):
            if iteration:
                config.update(
                    model='second', embedding_model='embed-two', reranker_model='rank-two'
                )
            assert await llm.generate_response([Message(role='user', content='hi')], Result) == {
                'answer': 'ok'
            }
            assert await embedder.create('hi') == [1.0, 2.0]
            assert await reranker.rank('hi', ['a']) == [('a', 0.8)]
        assert calls == [
            ('http://llm/v1/chat/completions', 'first'),
            ('http://infinity/embeddings', 'embed-one'),
            ('http://infinity/rerank', 'rank-one'),
            ('http://llm/v1/chat/completions', 'second'),
            ('http://infinity/embeddings', 'embed-two'),
            ('http://infinity/rerank', 'rank-two'),
        ]

        async def oauth_response(self, *args, **kwargs):
            assert self.auth is auth
            return {'answer': 'oauth'}

        monkeypatch.setattr(
            'graph_service.oauth_client.OpenAIOAuthClient._generate_response', oauth_response
        )
        auth.store.set('accounts', {'test': {'access_token': 'private-token'}})
        auth.store.set('active_account', 'test')
        config['llm_provider'] = 'oauth'
        assert await llm.generate_response([Message(role='user', content='hi')], Result) == {
            'answer': 'oauth'
        }
        assert len(calls) == 6
    assert auth.store.usage_summary()['totals']['total_tokens'] == 8
    assert llm.token_tracker.get_total_usage().input_tokens == 6


def test_http_mcp_mount_lists_original_tools_and_executes_search(runtime, monkeypatch):
    from graph_service import main, mcp_runtime
    from graph_service.config import Settings

    auth, config = runtime
    graph = SimpleNamespace(
        build_indices_and_constraints=AsyncMock(),
        search=AsyncMock(return_value=[]),
        add_episode=AsyncMock(),
        close=AsyncMock(),
    )
    settings = Settings(mcp_public_url='http://192.168.1.11:8123', openai_state_dir=auth.store.path.parent)

    @asynccontextmanager
    async def configured(settings, **kwargs):
        assert kwargs['live_config'] is True
        try:
            yield graph
        finally:
            await graph.close()

    monkeypatch.setattr(main, 'configured_graphiti', configured)
    monkeypatch.setattr(main, 'get_settings', lambda: settings)
    app = main.create_app(settings)
    headers = {'accept': 'application/json, text/event-stream'}
    session_id = None

    def rpc(client, method, params, identifier):
        nonlocal session_id
        response = client.post(
            '/mcp/',
            headers={**headers, **({'mcp-session-id': session_id} if session_id else {})},
            json={'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params},
        )
        assert response.status_code == 200, response.text
        if method == 'initialize':
            session_id = response.headers.get('mcp-session-id')
            assert session_id, 'initialize must return a Mcp-Session-Id for stateful MCP clients'
        return response.json()['result']

    with TestClient(app, base_url='http://192.168.1.11:8123') as client:
        result = rpc(
            client,
            'initialize',
            {
                'protocolVersion': '2025-03-26',
                'capabilities': {},
                'clientInfo': {'name': 'test', 'version': '1'},
            },
            1,
        )
        assert result['serverInfo']['name'] == 'Graphiti Agent Memory'
        tools = rpc(client, 'tools/list', {}, 2)['tools']
        names = {tool['name'] for tool in tools}
        assert {
            'add_memory',
            'search_memory_facts',
            'search_nodes',
            'get_status',
            'add_triplet',
        } <= names
        for identifier in (3, 4):
            result = rpc(
                client,
                'tools/call',
                {
                    'name': 'search_memory_facts',
                    'arguments': {'query': 'hello', 'group_ids': ['test']},
                },
                identifier,
            )
            assert not result.get('isError')
            assert result['structuredContent']['result']['facts'] == []
        for identifier in (7, 8):
            result = rpc(
                client,
                'tools/call',
                {
                    'name': 'add_memory',
                    'arguments': {'name': f'episode-{identifier}', 'episode_body': 'test only'},
                },
                identifier,
            )
            assert 'queued for processing' in json.dumps(result)
        queue = mcp_runtime.upstream.queue_service
        client.portal.call(queue._episode_queues['main'].join)
        assert [call.kwargs['name'] for call in graph.add_episode.await_args_list] == [
            'episode-7',
            'episode-8',
        ]
        assert client.get('/healthcheck').status_code == 200
        assert client.get('/admin').status_code == 200
        assert client.get('/mcp/health').status_code == 200
        assert (
            client.post(
                '/mcp/',
                headers={**headers, 'host': 'attacker.invalid'},
                json={'jsonrpc': '2.0', 'id': 5, 'method': 'tools/list'},
            ).status_code
            == 421
        )
        assert (
            client.post(
                '/mcp/',
                headers={**headers, 'origin': 'http://attacker.invalid'},
                json={'jsonrpc': '2.0', 'id': 9, 'method': 'tools/list'},
            ).status_code
            == 403
        )
        config['llm_provider'] = 'oauth'
        result = rpc(
            client,
            'tools/call',
            {
                'name': 'add_memory',
                'arguments': {
                    'name': 'blocked',
                    'episode_body': 'never enqueue without an account',
                },
            },
            6,
        )
        assert 'Sign in to OpenAI at /admin first' in json.dumps(result)
        assert graph.add_episode.await_count == 2
        # Installed MCP SDK rejects a terminated session BEFORE dispatching a write.
        # This is the boundary the client uses for its single safe 404 recovery.
        terminated = client.delete('/mcp/', headers={**headers, 'mcp-session-id': session_id})
        assert terminated.status_code == 200
        expired = client.post(
            '/mcp/',
            headers={**headers, 'mcp-session-id': session_id},
            json={
                'jsonrpc': '2.0',
                'id': 10,
                'method': 'tools/call',
                'params': {
                    'name': 'add_memory',
                    'arguments': {'name': 'expired-write', 'episode_body': 'never dispatch'},
                },
            },
        )
        assert expired.status_code == 404
        assert 'Session not found' in expired.text
        assert graph.add_episode.await_count == 2
    assert graph.search.await_count == 2
    graph.build_indices_and_constraints.assert_awaited_once()
    graph.close.assert_awaited_once()
    assert mcp_runtime.upstream.graphiti_service is None
    assert mcp_runtime.upstream.queue_service is None


@pytest.mark.asyncio
async def test_queue_preserves_order_and_shutdown_cleans_workers():
    from graph_service.mcp_runtime import upstream

    queue = upstream.QueueService()
    order = []

    async def first():
        order.append('first-start')
        await asyncio.sleep(0)
        order.append('first-end')

    async def second():
        order.append('second')

    await queue.add_episode_task('test', first)
    await queue.add_episode_task('test', second)
    await queue.shutdown()
    assert order == ['first-start', 'first-end', 'second']
    assert not queue.is_worker_running('test')
    assert not queue._worker_tasks
    # A reset queue may be initialized and used again without losing subsequent work.
    await queue.add_episode_task('test', second)
    await queue.shutdown()
    assert order[-1] == 'second'


@pytest.mark.asyncio
async def test_shutdown_cancels_active_work_before_closing_the_runtime():
    from graph_service.mcp_runtime import upstream

    queue = upstream.QueueService()
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def blocked():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    await queue.add_episode_task('test', blocked)
    await started.wait()
    await queue.shutdown(timeout=0.01)
    assert stopped.is_set()
    assert not queue._worker_tasks
