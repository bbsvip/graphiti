"""OpenAI's public-client OAuth flow and persistent, server-side runtime state."""

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import jwt
from filelock import AsyncFileLock

ISSUER = 'https://auth.openai.com'
RESOURCE = 'https://api.openai.com/v1'
TOKEN_ENDPOINT = ISSUER + '/api/accounts/oauth/token'
PLAN_SCOPE = 'chatgpt.tokens.use.direct'


class RuntimeStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        self.path = directory / 'openai-runtime.sqlite3'
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS usage (
                    created REAL NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                    cached_tokens INTEGER NOT NULL, status TEXT NOT NULL
                );
            """)
            db.execute(
                'INSERT OR IGNORE INTO state VALUES (?, ?)',
                ('host_id', json.dumps('urn:uuid:' + str(uuid4()))),
            )
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key: str, default: Any = None) -> Any:
        with self.connect() as db:
            row = db.execute('SELECT value FROM state WHERE key = ?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set(self, key: str, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO state VALUES (?, ?)', (key, json.dumps(value)))

    def record_usage(
        self,
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        status: str = 'completed',
    ):
        with self.connect() as db:
            db.execute(
                'INSERT INTO usage VALUES (?, ?, ?, ?, ?, ?, ?)',
                (time.time(), provider, model, input_tokens, output_tokens, cached_tokens, status),
            )

    def usage_summary(self):
        with self.connect() as db:
            totals = dict(
                db.execute("""
                SELECT COUNT(*) AS requests, COALESCE(SUM(input_tokens),0) AS input_tokens,
                COALESCE(SUM(output_tokens),0) AS output_tokens,
                COALESCE(SUM(cached_tokens),0) AS cached_tokens,
                COALESCE(SUM(input_tokens+output_tokens),0) AS total_tokens,
                COALESCE(SUM(status!='completed'),0) AS failures FROM usage
            """).fetchone()
            )
            daily = [
                dict(row)
                for row in db.execute(
                    """
                SELECT date(created, 'unixepoch') AS day,
                SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
                COUNT(*) AS requests FROM usage WHERE created >= ?
                GROUP BY day ORDER BY day
            """,
                    (time.time() - 30 * 86400,),
                )
            ]
            providers = [
                dict(row)
                for row in db.execute("""
                SELECT provider, model, COUNT(*) AS requests,
                SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens
                FROM usage GROUP BY provider, model ORDER BY requests DESC
            """)
            ]
        return {'totals': totals, 'daily': daily, 'providers': providers}

    def setup_password(self, password: str):
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310000).hex()
        with self.connect() as db:
            if db.execute("SELECT 1 FROM state WHERE key='admin_password'").fetchone():
                raise ValueError('Administrator already configured')
            db.execute(
                'INSERT INTO state VALUES (?, ?)',
                ('admin_password', json.dumps({'salt': salt, 'digest': digest})),
            )

    def check_password(self, password: str) -> bool:
        record = self.get('admin_password')
        if not record:
            return False
        digest = hashlib.pbkdf2_hmac(
            'sha256', password.encode(), record['salt'].encode(), 310000
        ).hex()
        return secrets.compare_digest(record['digest'], digest)

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self.set('session:' + hashlib.sha256(token.encode()).hexdigest(), time.time() + 43200)
        return token

    def valid_session(self, token: str) -> bool:
        expiry = self.get('session:' + hashlib.sha256(token.encode()).hexdigest(), 0)
        return expiry > time.time()


class OpenAIAuth:
    def __init__(self, store: RuntimeStore, callback_port: int):
        self.store = store
        self.redirect_uri = f'http://127.0.0.1:{callback_port}/auth/callback'
        self.lock_path = store.path.parent / 'oauth.lock'

    @property
    def lock(self):
        # Each operation needs a distinct lock instance: FileLock instances are reentrant.
        return AsyncFileLock(str(self.lock_path), mode=0o600)

    def active_account(self):
        active = self.store.get('active_account')
        return self.store.get('accounts', {}).get(active)

    def begin_login(self, account_id: str | None = None):
        account = self.store.get('accounts', {}).get(account_id) if account_id else None
        if account_id and not account:
            raise ValueError('Unknown account')
        verifier = secrets.token_urlsafe(64)
        attempt = {
            'state': secrets.token_urlsafe(32),
            'nonce': secrets.token_urlsafe(32),
            'verifier': verifier,
            'expires_at': time.time() + 600,
            'client_id': account['client_id'] if account else 'dynamic_agent_client',
            'account_id': account_id,
            'redirect_uri': self.redirect_uri,
        }
        self.store.set('pending_login', attempt)
        params = {
            'client_id': attempt['client_id'],
            'ext_agent_host_id': self.store.get('host_id'),
            'response_type': 'code',
            'redirect_uri': self.redirect_uri,
            'scope': 'openid profile email offline_access resource.invoke ' + PLAN_SCOPE,
            'resource': RESOURCE,
            'state': attempt['state'],
            'nonce': attempt['nonce'],
            'code_challenge_method': 'S256',
            'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip('='),
        }
        if account:
            if account.get('id_token'):
                params['id_token_hint'] = account['id_token']
        else:
            params['agent_name_hint'] = 'Graphiti'
        return {
            'url': ISSUER + '/api/accounts/authorize?' + urlencode(params),
            'expires_at': attempt['expires_at'],
        }

    async def _metadata(self, http: httpx.AsyncClient):
        response = await http.get(ISSUER + '/.well-known/openid-configuration')
        response.raise_for_status()
        metadata = response.json()
        if metadata.get('issuer') != ISSUER:
            raise ValueError('Invalid OpenAI issuer')
        return metadata

    @staticmethod
    def _trusted_url(url: str):
        parsed = httpx.URL(url)
        if parsed.scheme != 'https' or parsed.host != 'auth.openai.com':
            raise ValueError('Invalid OpenAI authentication endpoint')
        return url

    async def _validate_identity(self, http, tokens, client_id, nonce=None):
        metadata = await self._metadata(http)
        keys = await http.get(self._trusted_url(metadata['jwks_uri']))
        keys.raise_for_status()
        token = tokens['id_token']
        header = jwt.get_unverified_header(token)
        key = next(
            (key for key in keys.json()['keys'] if key.get('kid') == header.get('kid')), None
        )
        if key is None:
            raise ValueError('Unknown OpenAI signing key')
        claims = jwt.decode(
            token,
            jwt.PyJWK.from_dict(key).key,
            algorithms=['RS256'],
            audience=client_id,
            issuer=ISSUER,
            options={'require': ['exp', 'iss', 'aud', 'sub', 'iat']},
        )
        if nonce is not None and not secrets.compare_digest(str(claims.get('nonce', '')), nonce):
            raise ValueError('Invalid OpenAI nonce')
        return claims

    async def finish_login(self, query: dict):
        async with self.lock:
            attempt = self.store.get('pending_login')
            if (
                not attempt
                or attempt['expires_at'] < time.time()
                or not secrets.compare_digest(str(query.get('state', '')), attempt['state'])
            ):
                raise ValueError('Invalid or expired OAuth state')
            self.store.set('pending_login', None)  # A callback is consumed only once.
            if query.get('error'):
                raise ValueError('OpenAI authorization was declined; start a new sign-in')
            client_id = query.get('client_id', attempt['client_id'])
            if not client_id or client_id == 'dynamic_agent_client':
                raise ValueError('OpenAI did not return an issued client ID')
            if attempt['account_id'] and client_id != attempt['client_id']:
                raise ValueError('OpenAI client ID changed during reauthorization')
            if not query.get('code'):
                raise ValueError('OpenAI did not return an authorization code')
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.post(
                    TOKEN_ENDPOINT,
                    data={
                        'grant_type': 'authorization_code',
                        'client_id': client_id,
                        'code': query['code'],
                        'code_verifier': attempt['verifier'],
                        'redirect_uri': attempt['redirect_uri'],
                        'resource': RESOURCE,
                    },
                )
                if response.status_code != 200:
                    raise ValueError('OpenAI token exchange failed; start a new sign-in')
                tokens = response.json()
                claims = await self._validate_identity(http, tokens, client_id, attempt['nonce'])
            accounts = self.store.get('accounts', {})
            old = accounts.get(attempt['account_id'])
            if old and claims['sub'] != old['subject']:
                raise ValueError('OpenAI returned a different account')
            if PLAN_SCOPE not in tokens.get('scope', '').split():
                raise ValueError('ChatGPT plan usage permission was not granted')
            if not tokens.get('access_token') or not tokens.get('refresh_token'):
                raise ValueError('OpenAI returned incomplete credentials')
            accounts[client_id] = {
                **tokens,
                'client_id': client_id,
                'subject': claims['sub'],
                'email': claims.get('email', ''),
                'expires_at': time.time() + int(tokens['expires_in']),
            }
            with self.store.connect() as db:
                db.executemany(
                    'INSERT OR REPLACE INTO state VALUES (?, ?)',
                    [('accounts', json.dumps(accounts)), ('active_account', json.dumps(client_id))],
                )

    async def access_token(self):
        async with self.lock:
            account = self.active_account()
            if not account or not account.get('access_token'):
                raise ValueError('Sign in to OpenAI at /admin first')
            if account['expires_at'] > time.time() + 60:
                return account['access_token']
            if not account.get('refresh_token'):
                raise ValueError('OpenAI session expired; sign in again at /admin')
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.post(
                    TOKEN_ENDPOINT,
                    data={
                        'grant_type': 'refresh_token',
                        'client_id': account['client_id'],
                        'refresh_token': account['refresh_token'],
                        'resource': RESOURCE,
                    },
                )
                if response.status_code != 200:
                    raise ValueError('OpenAI session could not be renewed; sign in again at /admin')
                tokens = response.json()
                if tokens.get('id_token'):
                    claims = await self._validate_identity(http, tokens, account['client_id'])
                    if claims['sub'] != account['subject']:
                        raise ValueError('OpenAI refreshed a different account')
            if not tokens.get('access_token') or not tokens.get('refresh_token'):
                raise ValueError('OpenAI returned incomplete refreshed credentials')
            if PLAN_SCOPE not in tokens.get('scope', account['scope']).split():
                raise ValueError('ChatGPT plan permission was revoked')
            account.update(tokens)
            account['expires_at'] = time.time() + int(tokens['expires_in'])
            accounts = self.store.get('accounts', {})
            accounts[account['client_id']] = account
            self.store.set('accounts', accounts)
            return account['access_token']

    async def logout(self):
        async with self.lock:
            account = self.active_account()
            if not account:
                return {'revoked': True}
            revoked = False
            try:
                async with httpx.AsyncClient(timeout=30) as http:
                    metadata = await self._metadata(http)
                    response = await http.post(
                        self._trusted_url(metadata['revocation_endpoint']),
                        data={
                            'token': account['refresh_token'],
                            'token_type_hint': 'refresh_token',
                            'client_id': account['client_id'],
                        },
                    )
                    revoked = response.status_code == 200
            except (httpx.HTTPError, KeyError, ValueError):
                pass
            for key in ('access_token', 'refresh_token', 'id_token'):
                account.pop(key, None)
            accounts = self.store.get('accounts', {})
            accounts[account['client_id']] = account
            self.store.set('accounts', accounts)
            self.store.set('active_account', None)
            self.store.set('pending_login', None)
            return {'revoked': revoked}


@lru_cache
def get_openai_auth() -> OpenAIAuth:
    from graph_service.config import get_settings

    settings = get_settings()
    return OpenAIAuth(RuntimeStore(settings.openai_state_dir), settings.openai_callback_port)


def connection_settings() -> dict:
    from graph_service.config import get_settings

    settings = get_settings()
    saved = get_openai_auth().store.get('connections', {})
    return {
        'llm_provider': settings.llm_provider,
        'llm_base_url': settings.llm_base_url,
        'model': settings.model_name or '',
        'small_model': settings.model_name or '',
        'local_model_url': settings.local_model_url,
        'embedding_model': settings.embedding_model_name,
        'reranker_model': settings.reranker_model_name or '',
        **saved,
    }
