"""Responses adapter for ChatGPT plan usage; graph extraction contracts stay intact."""

import json
from typing import Any

from graphiti_core.llm_client import LLMClient, LLMConfig
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, ModelSize
from graphiti_core.prompts import Message
from openai import AsyncOpenAI
from pydantic import BaseModel

from graph_service.openai_auth import RESOURCE, OpenAIAuth, connection_settings


class OpenAIOAuthClient(LLMClient):
    def __init__(self, auth: OpenAIAuth):
        super().__init__(LLMConfig(), cache=False)
        self.auth = auth

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, Any]:
        settings = connection_settings()
        model = (
            settings.get('small_model') if model_size == ModelSize.small else settings.get('model')
        )
        if not model:
            raise ValueError('Choose an OpenAI model at /admin first')
        # This route rejects system items, temperature, and max_output_tokens.
        inputs = [
            {
                'role': 'developer' if m.role == 'system' else m.role,
                'content': self._clean_input(m.content),
            }
            for m in messages
        ]
        kwargs: dict[str, Any] = {'model': model, 'input': inputs, 'store': False}
        if response_model:
            kwargs['text_format'] = response_model
        else:
            kwargs['text'] = {'format': {'type': 'json_object'}}
        try:
            async with (
                AsyncOpenAI(
                    api_key=await self.auth.access_token(), base_url=RESOURCE, max_retries=0
                ) as client,
                client.responses.stream(**kwargs) as stream,
            ):
                response = None
                async for event in stream:
                    if (
                        event.type == 'response.completed'
                        or event.type == 'response.failed'
                        or event.type == 'response.incomplete'
                    ):
                        response = event.response
                if response is None:
                    raise ValueError('OpenAI stream ended without a terminal response')
            usage = response.usage
            input_tokens = usage.input_tokens if usage else 0
            output_tokens = usage.output_tokens if usage else 0
            details = usage.input_tokens_details if usage else None
            cached_tokens = details.cached_tokens if details else 0
            self.auth.store.record_usage(
                'llm',
                model,
                input_tokens,
                output_tokens,
                cached_tokens,
                status=response.status or 'unknown',
            )
            self.token_tracker.record(None, input_tokens, output_tokens)
        except Exception:
            # Failure without a terminal usage payload is not an estimate of billed tokens.
            self.auth.store.record_usage('llm', model, status='request_failed')
            raise
        if response.status != 'completed':
            code = response.error.code if response.error else response.status
            raise ValueError(f'OpenAI inference did not complete: {code}')
        if not response.output_text:
            raise ValueError('OpenAI returned no JSON output (or refused the request)')
        if response_model:
            return response_model.model_validate_json(response.output_text).model_dump()
        return json.loads(response.output_text)
