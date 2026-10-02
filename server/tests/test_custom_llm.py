import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from graphiti_core.llm_client.config import ModelSize
from graphiti_core.prompts import Message
from pydantic import BaseModel

from graph_service.openai_auth import OpenAIAuth, RuntimeStore


class Result(BaseModel):
    answer: str


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'size,expected_model', [(ModelSize.medium, 'main'), (ModelSize.small, 'small')]
)
async def test_custom_chat_uses_its_url_and_models_without_oauth_credentials(
    tmp_path, size, expected_model
):
    from graph_service.compatible_client import CompatibleLLMClient

    def handle(request):
        assert str(request.url) == 'http://local:8080/v1/chat/completions'
        assert 'authorization' not in request.headers
        payload = json.loads(request.content)
        assert payload['model'] == expected_model
        assert payload['messages'][0]['role'] == 'system'
        assert payload['response_format']['json_schema']['schema'] == Result.model_json_schema()
        return httpx.Response(
            200,
            json={
                'choices': [
                    {
                        'message': {'content': '```json\n{"answer":"yes"}\n```'},
                        'finish_reason': 'stop',
                    }
                ],
                'usage': {
                    'prompt_tokens': 10,
                    'completion_tokens': 2,
                    'prompt_tokens_details': {'cached_tokens': 3},
                },
            },
        )

    store = RuntimeStore(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = CompatibleLLMClient(http, 'http://local:8080/v1/', 'main', 'small', store)
        assert await client.generate_response(
            [Message(role='system', content='Extract JSON'), Message(role='user', content='hello')],
            Result,
            model_size=size,
        ) == {'answer': 'yes'}
    summary = store.usage_summary()
    assert summary['providers'][0]['provider'] == 'llm_custom'
    assert summary['totals']['total_tokens'] == 12
    assert summary['totals']['cached_tokens'] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'finish,content', [('length', '{"answer":"yes"}'), ('stop', '{"wrong":1}')]
)
async def test_custom_chat_rejects_incomplete_or_invalid_extraction_and_keeps_usage(
    tmp_path, finish, content
):
    from graph_service.compatible_client import CompatibleLLMClient

    def handle(request):
        return httpx.Response(
            200,
            json={
                'choices': [{'message': {'content': content}, 'finish_reason': finish}],
                'usage': {'prompt_tokens': 5, 'completion_tokens': 1},
            },
        )

    store = RuntimeStore(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = CompatibleLLMClient(http, 'http://local/v1', 'main', 'small', store)
        with pytest.raises(ValueError):
            await client._generate_response([Message(role='user', content='hello')], Result)
    assert store.usage_summary()['totals']['total_tokens'] == 6
    assert store.usage_summary()['totals']['failures'] == 1


@pytest.mark.asyncio
async def test_custom_provider_needs_no_chatgpt_account_and_keeps_infinity_clients(
    tmp_path, monkeypatch
):
    from graph_service import zep_graphiti
    from graph_service.compatible_client import CompatibleLLMClient
    from graph_service.config import Settings
    from graph_service.local_clients import InfinityEmbedder, InfinityReranker

    auth = OpenAIAuth(RuntimeStore(tmp_path), 8000)
    config = {
        'llm_provider': 'custom',
        'llm_base_url': 'http://local/v1',
        'model': 'main',
        'small_model': 'small',
        'local_model_url': 'http://infinity:7997',
        'embedding_model': 'BAAI/bge-m3',
        'reranker_model': 'reranker',
    }
    monkeypatch.setattr(zep_graphiti, 'get_openai_auth', lambda: auth)
    monkeypatch.setattr(zep_graphiti, 'connection_settings', lambda: config)

    class Graph:
        def __init__(self, **kwargs):
            assert isinstance(kwargs['llm_client'], CompatibleLLMClient)
            assert isinstance(kwargs['embedder'], InfinityEmbedder)
            assert kwargs['embedder'].base_url == config['local_model_url']
            assert kwargs['embedder'].model == config['embedding_model']
            assert isinstance(kwargs['cross_encoder'], InfinityReranker)
            assert kwargs['cross_encoder'].model == config['reranker_model']

        async def close(self):
            pass

    monkeypatch.setattr(zep_graphiti, 'ZepGraphiti', Graph)
    settings = Settings(
        _env_file=None, neo4j_uri='bolt://unused', neo4j_user='neo4j', neo4j_password='test'
    )
    dependency = zep_graphiti.get_graphiti(settings)
    assert isinstance(await anext(dependency), Graph)
    await dependency.aclose()


def test_custom_catalog_accepts_draft_url_and_save_survives_restart(tmp_path, monkeypatch):
    from graph_service import openai_auth
    from graph_service.config import Settings
    from graph_service.routers import admin

    auth = OpenAIAuth(RuntimeStore(tmp_path), 8000)
    monkeypatch.setattr(admin, 'get_openai_auth', lambda: auth)
    monkeypatch.setattr(openai_auth, 'get_openai_auth', lambda: auth)
    monkeypatch.setattr('graph_service.config.get_settings', lambda: Settings(_env_file=None))
    requests = []

    def handle(request):
        requests.append(request)
        assert 'authorization' not in request.headers
        if request.url.host == 'custom':
            assert request.url.path == '/v1/models'
            return httpx.Response(200, json={'data': [{'id': 'main'}, {'id': 'small'}]})
        assert request.url.path == '/models'
        return httpx.Response(
            200,
            json={
                'data': [
                    {'id': 'BAAI/bge-m3', 'capabilities': ['embed']},
                    {'id': 'reranker', 'capabilities': ['rerank']},
                ]
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        admin.httpx,
        'AsyncClient',
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    app = FastAPI()
    app.include_router(admin.router)
    with TestClient(app) as client:
        headers = {'x-graphiti-admin': '1'}
        client.post('/admin/api/setup', headers=headers, json={'password': 'secure-test-password'})
        catalog = client.get(
            '/admin/api/models',
            params={'llm_provider': 'custom', 'llm_base_url': 'http://custom:8080/v1/'},
        )
        assert catalog.status_code == 200
        assert catalog.json()['llm'] == [
            {'id': 'main', 'name': 'main'},
            {'id': 'small', 'name': 'small'},
        ]
        assert not catalog.json()['errors']
        body = {
            'llm_provider': 'custom',
            'llm_base_url': 'http://custom:8080/v1/',
            'model': 'main',
            'small_model': 'small',
            'local_model_url': 'http://192.168.1.11:7997',
            'embedding_model': 'BAAI/bge-m3',
            'reranker_model': 'reranker',
        }
        saved = client.post('/admin/api/connections', headers=headers, json=body)
        assert saved.status_code == 200, saved.text
        assert auth.active_account() is None
        config = client.get('/admin/api/status').json()['connections']
        assert config['llm_provider'] == 'custom'
        assert config['llm_base_url'] == 'http://custom:8080/v1'
        assert config['embedding_model'] == body['embedding_model']
        body['llm_base_url'] = 'http://user:secret@custom/v1'
        assert client.post('/admin/api/connections', headers=headers, json=body).status_code == 400
        body['llm_base_url'] = 'http://custom/v1?token=secret'
        assert client.post('/admin/api/connections', headers=headers, json=body).status_code == 400
    restarted = RuntimeStore(tmp_path).get('connections')
    assert restarted['llm_provider'] == 'custom'
    assert all(request.url.host != 'api.openai.com' for request in requests)


def test_old_connection_state_defaults_to_oauth(tmp_path, monkeypatch):
    from graph_service import openai_auth
    from graph_service.config import Settings

    auth = OpenAIAuth(RuntimeStore(tmp_path), 8000)
    auth.store.set('connections', {'model': 'eligible', 'embedding_model': 'BAAI/bge-m3'})
    monkeypatch.setattr(openai_auth, 'get_openai_auth', lambda: auth)
    monkeypatch.setattr('graph_service.config.get_settings', lambda: Settings(_env_file=None))
    assert openai_auth.connection_settings()['llm_provider'] == 'oauth'
