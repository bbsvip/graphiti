"""Keyless Chat Completions adapter for local OpenAI-compatible LLM servers."""

import json
import re

import httpx
from graphiti_core.llm_client import LLMClient, LLMConfig
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, ModelSize
from graphiti_core.llm_client.errors import RateLimitError
from graphiti_core.prompts import Message
from pydantic import BaseModel

from graph_service.openai_auth import RuntimeStore


def normalize_llm_base_url(value: str) -> str:
    url = httpx.URL(value)
    if (
        url.scheme not in ('http', 'https')
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
    ):
        raise ValueError('Use an HTTP(S) LLM base URL without credentials, query or fragment')
    return str(url).rstrip('/')


class CompatibleLLMClient(LLMClient):
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        model: str,
        small_model: str,
        store: RuntimeStore,
    ):
        super().__init__(LLMConfig(model=model, small_model=small_model), cache=False)
        self.http = http
        self.base_url = normalize_llm_base_url(base_url)
        self.store = store

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict:
        model = self.small_model if model_size == ModelSize.small else self.model
        if not model:
            raise ValueError('Choose a custom LLM model at /admin first')
        usage = {}
        try:
            response = await self.http.post(
                self.base_url + '/chat/completions',
                json={
                    'model': model,
                    'messages': [
                        {'role': m.role, 'content': self._clean_input(m.content)} for m in messages
                    ],
                    'temperature': self.temperature,
                    'max_tokens': max_tokens,
                    'stream': False,
                    'response_format': (
                        {
                            'type': 'json_schema',
                            'json_schema': {
                                'name': response_model.__name__,
                                'schema': response_model.model_json_schema(),
                            },
                        }
                        if response_model
                        else {'type': 'json_object'}
                    ),
                },
            )
            if response.status_code == 429:
                raise RateLimitError
            response.raise_for_status()
            body = response.json()
            usage = body.get('usage') or {}
            choice = body['choices'][0]
            if choice.get('finish_reason') != 'stop':
                raise ValueError('Custom LLM inference did not complete')
            text = (choice['message'].get('content') or '').strip()
            # Match the generic core client's handling of local models' JSON fences.
            if text.startswith('```'):
                text = re.sub(r'^```[a-zA-Z0-9_-]*[ \t]*\r?\n?', '', text)
                text = re.sub(r'\r?\n?```[ \t]*$', '', text).strip()
            if not text:
                raise ValueError('Custom LLM returned no JSON output (or refused the request)')
            result = (
                response_model.model_validate_json(text).model_dump()
                if response_model
                else json.loads(text)
            )
            if not isinstance(result, dict):
                raise ValueError('Custom LLM must return a JSON object')
        except Exception:
            self._record_usage(model, usage, 'request_failed')
            raise
        self._record_usage(model, usage, 'completed')
        self.token_tracker.record(
            None, usage.get('prompt_tokens', 0), usage.get('completion_tokens', 0)
        )
        return result

    def _record_usage(self, model: str, usage: dict, status: str):
        details = usage.get('prompt_tokens_details') or {}
        self.store.record_usage(
            'llm_custom',
            model,
            usage.get('prompt_tokens', 0),
            usage.get('completion_tokens', 0),
            details.get('cached_tokens', 0),
            status=status,
        )
