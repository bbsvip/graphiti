import asyncio
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from graph_service.openai_auth import ISSUER, PLAN_SCOPE, OpenAIAuth, RuntimeStore
from graph_service.routers import admin


@pytest.fixture
def auth(tmp_path):
    return OpenAIAuth(RuntimeStore(tmp_path), 8000)


def auth_transport(auth, monkeypatch, scope=PLAN_SCOPE, nonce_override=None, wrong_key=False):
    attempt = auth.store.get('pending_login')
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk['kid'] = 'key1'
    claims = {
        'iss': ISSUER,
        'aud': 'oaiapp_graphiti',
        'sub': 'user1',
        'exp': time.time() + 3600,
        'iat': time.time(),
        'email': 'test@example.com',
        'nonce': nonce_override or attempt['nonce'],
    }
    key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048) if wrong_key else signing_key
    )
    id_token = jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'key1'})
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == '/api/accounts/oauth/token':
            body = parse_qs(request.content.decode())
            assert body['client_id'] == ['oaiapp_graphiti']
            assert body['redirect_uri'] == [attempt['redirect_uri']]
            assert body['code_verifier'] == [attempt['verifier']]
            return httpx.Response(
                200,
                json={
                    'access_token': 'access',
                    'refresh_token': 'refresh',
                    'id_token': id_token,
                    'scope': scope,
                    'expires_in': 3600,
                },
            )
        if request.url.path == '/.well-known/openid-configuration':
            return httpx.Response(200, json={'issuer': ISSUER, 'jwks_uri': ISSUER + '/jwks'})
        return httpx.Response(200, json={'keys': [jwk]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        'graph_service.openai_auth.httpx.AsyncClient',
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    return calls


@pytest.mark.asyncio
async def test_verified_callback_saves_credentials_and_cannot_be_replayed(auth, monkeypatch):
    auth.begin_login()
    query = {
        'state': auth.store.get('pending_login')['state'],
        'code': 'code',
        'client_id': 'oaiapp_graphiti',
    }
    auth_transport(auth, monkeypatch)
    await auth.finish_login(query)
    assert auth.active_account()['subject'] == 'user1'
    assert await auth.access_token() == 'access'
    with pytest.raises(ValueError, match='state'):
        await auth.finish_login(query)
    restarted = OpenAIAuth(RuntimeStore(auth.store.path.parent), 8000)
    assert restarted.active_account()['client_id'] == 'oaiapp_graphiti'
    returning = parse_qs(urlsplit(restarted.begin_login('oaiapp_graphiti')['url']).query)
    assert returning['client_id'] == ['oaiapp_graphiti']
    assert 'agent_name_hint' not in returning


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['nonce', 'signature', 'scope'])
async def test_unverified_or_unconsented_login_never_activates_account(auth, monkeypatch, case):
    auth.begin_login()
    query = {
        'state': auth.store.get('pending_login')['state'],
        'code': 'code',
        'client_id': 'oaiapp_graphiti',
    }
    auth_transport(
        auth,
        monkeypatch,
        nonce_override='wrong' if case == 'nonce' else None,
        wrong_key=case == 'signature',
        scope='openid' if case == 'scope' else PLAN_SCOPE,
    )
    with pytest.raises((ValueError, jwt.InvalidTokenError)):
        await auth.finish_login(query)
    assert auth.active_account() is None


@pytest.mark.asyncio
async def test_concurrent_calls_refresh_once_and_persist_rotated_token(auth, monkeypatch):
    auth.store.set(
        'accounts',
        {
            'account': {
                'client_id': 'account',
                'subject': 'user1',
                'access_token': 'old',
                'refresh_token': 'refresh1',
                'expires_at': 0,
                'scope': PLAN_SCOPE,
            }
        },
    )
    auth.store.set('active_account', 'account')
    calls = []

    async def handle(request):
        calls.append(request)
        await asyncio.sleep(0.05)
        assert parse_qs(request.content.decode())['refresh_token'] == ['refresh1']
        return httpx.Response(
            200,
            json={
                'access_token': 'new',
                'refresh_token': 'refresh2',
                'expires_in': 3600,
                'scope': PLAN_SCOPE,
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        'graph_service.openai_auth.httpx.AsyncClient',
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    assert await asyncio.gather(auth.access_token(), auth.access_token()) == ['new', 'new']
    assert len(calls) == 1
    assert auth.active_account()['refresh_token'] == 'refresh2'


def test_admin_requires_session_and_csrf_header_and_never_exposes_tokens(auth, monkeypatch):
    monkeypatch.setattr(admin, 'get_openai_auth', lambda: auth)
    app = FastAPI()
    app.include_router(admin.router)
    app.include_router(admin.callback_router)
    with TestClient(app) as client:
        assert client.get('/admin/api/status').status_code == 401
        assert (
            client.post('/admin/api/setup', json={'password': 'secure-test-password'}).status_code
            == 403
        )
        headers = {'x-graphiti-admin': '1'}
        response = client.post(
            '/admin/api/setup', headers=headers, json={'password': 'secure-test-password'}
        )
        assert response.status_code == 200
        assert 'httponly' in response.headers['set-cookie'].lower()
        assert (
            client.post(
                '/admin/api/setup', headers=headers, json={'password': 'new-password'}
            ).status_code
            == 409
        )
        auth.store.set(
            'accounts',
            {
                'account': {
                    'client_id': 'account',
                    'email': '<script>bad</script>',
                    'access_token': 'TOP_SECRET_TOKEN',
                    'refresh_token': 'SECRET_REFRESH',
                }
            },
        )
        response = client.get('/admin/api/status')
        assert response.status_code == 200
        assert 'TOP_SECRET_TOKEN' not in response.text
        assert 'SECRET_REFRESH' not in response.text
        assert (
            client.post(
                '/admin/api/openai/logout',
                headers={**headers, 'origin': 'https://attacker'},
                json={},
            ).status_code
            == 403
        )
        assert client.post('/admin/api/session/logout', headers=headers, json={}).status_code == 200
        assert client.get('/admin/api/status').status_code == 401


class Result(BaseModel):
    answer: str


def test_manual_callback_requires_admin_and_reuses_verified_oauth_flow(auth, monkeypatch):
    auth = OpenAIAuth(auth.store, 8123)
    monkeypatch.setattr(admin, 'get_openai_auth', lambda: auth)
    app = FastAPI()
    app.include_router(admin.router)
    auth.begin_login()
    query = {
        'state': auth.store.get('pending_login')['state'],
        'code': 'private-code',
        'client_id': 'oaiapp_graphiti',
    }
    calls = auth_transport(auth, monkeypatch)
    body = {'callback_url': auth.redirect_uri + '?' + urlencode(query)}
    with TestClient(app) as client:
        headers = {'x-graphiti-admin': '1'}
        assert (
            client.post('/admin/api/openai/callback', headers=headers, json=body).status_code == 401
        )
        assert not calls
        client.post('/admin/api/setup', headers=headers, json={'password': 'secure-test-password'})
        assert client.post('/admin/api/openai/callback', json=body).status_code == 403
        assert not calls
        response = client.post('/admin/api/openai/callback', headers=headers, json=body)
        assert response.status_code == 200
        assert response.json() == {'connected': True}
        assert response.headers['cache-control'] == 'no-store'
        assert auth.active_account()['subject'] == 'user1'
        assert (
            RuntimeStore(auth.store.path.parent).get('accounts')['oaiapp_graphiti']['access_token']
            == 'access'
        )
        replay = client.post('/admin/api/openai/callback', headers=headers, json=body)
        assert replay.status_code == 400
        assert 'private-code' not in replay.text
        assert query['state'] not in replay.text
        assert 'refresh' not in response.text
        assert sum(request.url.path == '/api/accounts/oauth/token' for request in calls) == 1


@pytest.mark.parametrize(
    'url',
    [
        'http://192.168.1.11:8000/auth/callback?state=s&code=c',
        'http://localhost:8000/auth/callback?state=s&code=c',
        'http://127.0.0.1:8123/auth/callback?state=s&code=c',
        'http://127.0.0.1:8000/callback?state=s&code=c',
        'http://user:password@127.0.0.1:8000/auth/callback?state=s&code=c',
        'http://127.0.0.1:8000/auth/callback?state=s&code=c#fragment',
        'http://127.0.0.1:8000/auth/callback?state=s&state=other&code=c',
    ],
)
def test_manual_callback_rejects_wrong_uri_or_ambiguous_query_before_exchange(
    auth, monkeypatch, url
):
    monkeypatch.setattr(admin, 'get_openai_auth', lambda: auth)
    called = []

    async def finish(query):
        called.append(query)

    monkeypatch.setattr(auth, 'finish_login', finish)
    app = FastAPI()
    app.include_router(admin.router)
    with TestClient(app) as client:
        headers = {'x-graphiti-admin': '1'}
        client.post('/admin/api/setup', headers=headers, json={'password': 'secure-test-password'})
        response = client.post(
            '/admin/api/openai/callback', headers=headers, json={'callback_url': url}
        )
        assert response.status_code == 400
        assert not called
        assert auth.active_account() is None


def test_manual_callback_invalid_state_leaves_pending_attempt_and_hides_code(auth, monkeypatch):
    monkeypatch.setattr(admin, 'get_openai_auth', lambda: auth)
    auth.begin_login()
    pending = auth.store.get('pending_login')
    app = FastAPI()
    app.include_router(admin.router)
    with TestClient(app) as client:
        headers = {'x-graphiti-admin': '1'}
        client.post('/admin/api/setup', headers=headers, json={'password': 'secure-test-password'})
        response = client.post(
            '/admin/api/openai/callback',
            headers=headers,
            json={
                'callback_url': auth.redirect_uri
                + '?state=wrong&code=private-code&client_id=oaiapp_graphiti'
            },
        )
        assert response.status_code == 400
        assert 'private-code' not in response.text
        assert auth.store.get('pending_login') == pending
        assert auth.active_account() is None


@pytest.mark.asyncio
async def test_failed_remote_revocation_still_clears_local_credentials(auth, monkeypatch):
    auth.store.set(
        'accounts',
        {
            'account': {
                'client_id': 'account',
                'subject': 'user1',
                'access_token': 'secret-access',
                'refresh_token': 'secret-refresh',
                'id_token': 'secret-id',
            }
        },
    )
    auth.store.set('active_account', 'account')

    def handle(request):
        raise httpx.ConnectError('offline', request=request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        'graph_service.openai_auth.httpx.AsyncClient',
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    assert await auth.logout() == {'revoked': False}
    account = auth.store.get('accounts')['account']
    assert 'access_token' not in account
    assert 'refresh_token' not in account
    assert 'id_token' not in account
    assert auth.active_account() is None


@pytest.mark.asyncio
async def test_declined_authorization_never_exchanges_code_or_replaces_active_account(auth):
    auth.store.set('accounts', {'account': {'client_id': 'account', 'access_token': 'existing'}})
    auth.store.set('active_account', 'account')
    auth.begin_login()
    state = auth.store.get('pending_login')['state']
    with pytest.raises(ValueError, match='declined'):
        await auth.finish_login({'state': state, 'error': 'access_denied'})
    assert auth.active_account()['access_token'] == 'existing'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['completed', 'incomplete', 'failed'])
async def test_oauth_inference_preserves_schema_and_requires_completed_status(
    auth, monkeypatch, status
):
    from graphiti_core.prompts import Message

    from graph_service import oauth_client

    calls = []
    response = SimpleNamespace(
        status=status,
        error=None,
        output_text='{"answer":"yes"}',
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=2, input_tokens_details=SimpleNamespace(cached_tokens=3)
        ),
    )

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def __aiter__(self):
            return self.events()

        async def events(self):
            yield SimpleNamespace(type='response.' + status, response=response)

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['api_key'] == 'access'
            assert kwargs['base_url'] == 'https://api.openai.com/v1'
            self.responses = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, **kwargs):
            calls.append(kwargs)
            return Stream()

    async def token():
        return 'access'

    monkeypatch.setattr(auth, 'access_token', token)
    monkeypatch.setattr(oauth_client, 'AsyncOpenAI', Client)
    monkeypatch.setattr(
        oauth_client,
        'connection_settings',
        lambda: {'model': 'eligible', 'small_model': 'eligible'},
    )
    client = oauth_client.OpenAIOAuthClient(auth)
    messages = [
        Message(role='system', content='Extract JSON'),
        Message(role='user', content='hello'),
    ]
    if status == 'completed':
        assert await client._generate_response(messages, Result) == {'answer': 'yes'}
    else:
        with pytest.raises(ValueError, match='did not complete'):
            await client._generate_response(messages, Result)
    assert calls[0]['input'][0]['role'] == 'developer'
    assert calls[0]['store'] is False
    assert calls[0]['text_format'] is Result
    assert 'temperature' not in calls[0]
    assert 'max_output_tokens' not in calls[0]
    assert auth.store.usage_summary()['totals']['total_tokens'] == 12


@pytest.mark.asyncio
async def test_infinity_reranking_preserves_duplicate_passages_and_sorts_scores(auth):
    from graph_service.local_clients import InfinityReranker

    def handle(request):
        assert 'authorization' not in request.headers
        assert json.loads(request.content)['documents'] == ['a', 'b', 'a']
        return httpx.Response(
            200,
            json={
                'results': [
                    {'index': 1, 'relevance_score': 0.8},
                    {'index': 2, 'relevance_score': 0.6},
                    {'index': 0, 'relevance_score': 0.1},
                ],
                'usage': {'prompt_tokens': 5},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        reranker = InfinityReranker(http, 'http://local', 'rerank', auth.store)
        assert await reranker.rank('query', ['a', 'b', 'a']) == [('b', 0.8), ('a', 0.6), ('a', 0.1)]


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['completed', 'failed', 'incomplete'])
async def test_real_sdk_sse_records_terminal_usage_without_an_api_key(auth, monkeypatch, status):
    from graphiti_core.prompts import Message
    from openai import AsyncOpenAI

    from graph_service import oauth_client

    def handle(request):
        payload = json.loads(request.content)
        assert request.headers['authorization'] == 'Bearer oauth-token'
        assert payload['stream'] is True
        assert payload['store'] is False
        assert payload['text']['format']['type'] == 'json_schema'
        assert 'max_output_tokens' not in payload
        response = {
            'id': 'resp_test',
            'created_at': int(time.time()),
            'model': 'eligible',
            'object': 'response',
            'status': status,
            'error': None,
            'incomplete_details': None,
            'instructions': None,
            'parallel_tool_calls': True,
            'tool_choice': 'auto',
            'tools': [],
            'output': [
                {
                    'type': 'message',
                    'id': 'msg_test',
                    'status': 'completed',
                    'role': 'assistant',
                    'content': [
                        {'type': 'output_text', 'text': '{"answer":"yes"}', 'annotations': []}
                    ],
                }
            ],
            'usage': {
                'input_tokens': 10,
                'output_tokens': 2,
                'total_tokens': 12,
                'input_tokens_details': {'cached_tokens': 3},
                'output_tokens_details': {'reasoning_tokens': 0},
            },
        }
        created = {**response, 'output': [], 'status': 'in_progress', 'usage': None}
        events = [
            {'type': 'response.created', 'response': created, 'sequence_number': 0},
            {'type': 'response.' + status, 'response': response, 'sequence_number': 1},
        ]
        content = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=content)

    def client(**kwargs):
        return AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )

    async def token():
        return 'oauth-token'

    monkeypatch.setattr(auth, 'access_token', token)
    monkeypatch.setattr(oauth_client, 'AsyncOpenAI', client)
    monkeypatch.setattr(oauth_client, 'connection_settings', lambda: {'model': 'eligible'})
    llm = oauth_client.OpenAIOAuthClient(auth)
    if status == 'completed':
        assert await llm._generate_response(
            [Message(role='user', content='JSON please')], Result
        ) == {'answer': 'yes'}
    else:
        with pytest.raises(ValueError, match='did not complete'):
            await llm._generate_response([Message(role='user', content='JSON please')], Result)
    assert auth.store.usage_summary()['totals']['total_tokens'] == 12
