"""Administration for OpenAI login, local connections and application usage."""

from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import AnyHttpUrl, BaseModel, Field

from graph_service.compatible_client import normalize_llm_base_url
from graph_service.openai_auth import (
    RESOURCE,
    connection_settings,
    get_openai_auth,
)

ASSETS = Path(__file__).parent.parent / 'static'
COOKIE = 'graphiti_admin'


def check_mutation(request: Request):
    if request.method != 'POST':
        return
    if request.headers.get('x-graphiti-admin') != '1':
        raise HTTPException(403, 'Missing administration request header')
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, 'Cross-origin administration is not allowed')


def require_admin(request: Request):
    token = request.cookies.get(COOKIE, '')
    if not token or not get_openai_auth().store.valid_session(token):
        raise HTTPException(401, 'Sign in to the administration page first')


router = APIRouter(
    prefix='/admin', tags=['OpenAI administration'], dependencies=[Depends(check_mutation)]
)
protected = APIRouter(dependencies=[Depends(require_admin)])
callback_router = APIRouter()


class PasswordInput(BaseModel):
    password: str = Field(min_length=10, max_length=256)


class ConnectionInput(BaseModel):
    llm_provider: Literal['oauth', 'custom'] = 'oauth'
    llm_base_url: AnyHttpUrl | None = None
    model: str = Field(max_length=128)
    small_model: str = Field(max_length=128)
    local_model_url: AnyHttpUrl
    embedding_model: str = Field(min_length=1, max_length=256)
    reranker_model: str = Field(default='', max_length=256)


class AccountInput(BaseModel):
    account_id: str | None = None


class CallbackInput(BaseModel):
    callback_url: str


@router.get('', include_in_schema=False)
def page():
    return FileResponse(ASSETS / 'admin.html', headers={'Cache-Control': 'no-store'})


@router.get('/assets/{name}', include_in_schema=False)
def asset(name: str):
    if name not in ('admin.css', 'admin.js'):
        raise HTTPException(404)
    return FileResponse(ASSETS / name)


@router.get('/api/session')
def session(request: Request):
    store = get_openai_auth().store
    return {
        'needs_setup': not bool(store.get('admin_password')),
        'authenticated': store.valid_session(request.cookies.get(COOKIE, '')),
    }


def set_session(response: Response, request: Request):
    response.set_cookie(
        COOKIE,
        get_openai_auth().store.new_session(),
        max_age=43200,
        httponly=True,
        secure=request.url.scheme == 'https',
        samesite='strict',
        path='/admin',
    )
    response.headers['Cache-Control'] = 'no-store'


@router.post('/api/setup')
def setup(body: PasswordInput, response: Response, request: Request):
    try:
        get_openai_auth().store.setup_password(body.password)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    set_session(response, request)
    return {'authenticated': True}


@router.post('/api/login')
def login(body: PasswordInput, response: Response, request: Request):
    if not get_openai_auth().store.check_password(body.password):
        raise HTTPException(401, 'Incorrect administrator password')
    set_session(response, request)
    return {'authenticated': True}


@protected.post('/api/session/logout')
def logout_session(request: Request, response: Response):
    import hashlib

    token = request.cookies.get(COOKIE, '')
    get_openai_auth().store.set('session:' + hashlib.sha256(token.encode()).hexdigest(), 0)
    response.delete_cookie(COOKIE, path='/admin')
    return {'authenticated': False}


@protected.get('/api/status')
def status(response: Response):
    auth = get_openai_auth()
    accounts = auth.store.get('accounts', {})
    response.headers['Cache-Control'] = 'no-store'
    return {
        'accounts': [
            {
                'id': key,
                'email': account.get('email', ''),
                'connected': bool(account.get('access_token')),
            }
            for key, account in accounts.items()
        ],
        'active_account': auth.store.get('active_account'),
        'connections': connection_settings(),
        'callback_uri': auth.redirect_uri,
        'usage': auth.store.usage_summary(),
    }


@protected.post('/api/openai/login')
def openai_login(body: AccountInput):
    try:
        return get_openai_auth().begin_login(body.account_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@protected.post('/api/openai/select')
async def select_account(body: AccountInput):
    auth = get_openai_auth()
    async with auth.lock:
        account = auth.store.get('accounts', {}).get(body.account_id)
        if not account or not account.get('access_token'):
            raise HTTPException(400, 'This account needs to sign in again')
        auth.store.set('active_account', body.account_id)
    return {'selected': True}


@protected.post('/api/openai/callback')
async def manual_callback(body: CallbackInput, response: Response):
    auth = get_openai_auth()
    response.headers['Cache-Control'] = 'no-store'
    try:
        if len(body.callback_url) > 16384:
            raise ValueError('Callback is too long')
        parsed = urlsplit(body.callback_url.strip())
        if (
            f'{parsed.scheme}://{parsed.netloc}{parsed.path}' != auth.redirect_uri
            or parsed.fragment
        ):
            raise ValueError('Callback URI does not match this deployment')
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        query = dict(pairs)
        if len(query) != len(pairs):
            raise ValueError('Duplicate OAuth parameters')
        # Use the original attempt's state, PKCE verifier and redirect URI; never fetch this URL.
        await auth.finish_login(query)
    except Exception as exc:
        # Do not expose pasted codes, state or upstream token responses in an API error.
        raise HTTPException(
            400,
            'OpenAI sign-in failed or expired. Copy the complete callback URL '
            'from the latest sign-in attempt, or start a new sign-in.',
            headers={'Cache-Control': 'no-store'},
        ) from exc
    return {'connected': True}


@protected.post('/api/openai/logout')
async def openai_logout():
    return await get_openai_auth().logout()


async def openai_models():
    token = await get_openai_auth().access_token()
    async with httpx.AsyncClient(timeout=30) as http:
        result = await http.get(RESOURCE + '/models', headers={'Authorization': 'Bearer ' + token})
        if result.status_code != 200:
            raise HTTPException(502, 'OpenAI model catalog is unavailable')
    return [
        {'id': m['slug'], 'name': m['display_name']}
        for m in result.json()['models']
        if m.get('visibility') == 'list'
    ]


async def custom_models(base_url: str):
    try:
        base_url = normalize_llm_base_url(base_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.get(base_url + '/models')
        response.raise_for_status()
    return [{'id': m['id'], 'name': m['id']} for m in response.json()['data']]


@protected.get('/api/models')
async def models(
    llm_provider: Literal['oauth', 'custom'] | None = None,
    llm_base_url: AnyHttpUrl | None = None,
):
    config = connection_settings()
    provider = llm_provider or config['llm_provider']
    result: dict = {'llm': [], 'local': [], 'errors': []}
    try:
        result['llm'] = (
            await custom_models(str(llm_base_url) if llm_base_url else config['llm_base_url'])
            if provider == 'custom'
            else await openai_models()
        )
    except (ValueError, KeyError, httpx.HTTPError, HTTPException):
        result['errors'].append(
            'Cannot read the custom LLM model catalog. Check its base URL (including /v1).'
            if provider == 'custom'
            else 'Sign in to OpenAI to load eligible LLM models.'
        )
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            response = await http.get(config['local_model_url'].rstrip('/') + '/models')
            response.raise_for_status()
            result['local'] = response.json()['data']
    except (httpx.HTTPError, KeyError, ValueError):
        result['errors'].append('Infinity model service is unavailable.')
    return result


@protected.post('/api/connections')
async def connections(body: ConnectionInput):
    if body.local_model_url.username or body.local_model_url.password:
        raise HTTPException(400, 'Use a local model URL without credentials')
    base_url = ''
    if body.llm_base_url:
        try:
            base_url = normalize_llm_base_url(str(body.llm_base_url))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if body.llm_provider == 'custom' and not base_url:
        raise HTTPException(400, 'Enter a custom LLM base URL (including /v1)')
    if not body.model or not body.small_model:
        raise HTTPException(400, 'Choose the primary and small LLM models before saving')
    if body.llm_provider == 'custom':
        try:
            available = {m['id'] for m in await custom_models(base_url)}
        except (ValueError, KeyError, httpx.HTTPError) as exc:
            raise HTTPException(400, 'Cannot read the custom LLM model catalog') from exc
        if body.model not in available or body.small_model not in available:
            raise HTTPException(400, 'Choose LLM models from the custom server catalog')
    else:
        try:
            available = {m['id'] for m in await openai_models()}
        except (ValueError, httpx.HTTPError) as exc:
            raise HTTPException(400, 'Sign in to OpenAI before selecting models') from exc
        if body.model not in available or body.small_model not in available:
            raise HTTPException(400, 'Choose LLM models from the signed-in account catalog')
    async with httpx.AsyncClient(timeout=10) as http:
        try:
            result = await http.get(str(body.local_model_url).rstrip('/') + '/models')
            result.raise_for_status()
            available_local = {m['id']: m.get('capabilities', []) for m in result.json()['data']}
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise HTTPException(400, 'Cannot read the Infinity model catalog') from exc
    if 'embed' not in available_local.get(body.embedding_model, []):
        raise HTTPException(400, 'Selected model does not support embeddings')
    if body.reranker_model and 'rerank' not in available_local.get(body.reranker_model, []):
        raise HTTPException(400, 'Selected model does not support reranking')
    if not body.reranker_model:
        raise HTTPException(400, 'Choose an Infinity reranker model before saving')
    saved = body.model_dump(mode='json')
    saved['llm_base_url'] = base_url
    get_openai_auth().store.set('connections', saved)
    return {'saved': True}


@callback_router.get('/auth/callback', include_in_schema=False)
async def callback(request: Request):
    try:
        await get_openai_auth().finish_login(dict(request.query_params))
    except Exception:
        # Never render upstream errors, callback codes, JWTs, or credentials.
        return HTMLResponse(
            '<meta charset="utf-8"><p>OpenAI sign-in failed or expired. '
            'Return to the administration page and try again.</p>',
            status_code=400,
            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'},
        )
    return HTMLResponse(
        '<meta charset="utf-8"><p>OpenAI connected. You can close this window '
        'and return to the administration page to choose a model.</p>',
        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'},
    )


router.include_router(protected)
