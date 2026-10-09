"""Responses adapter: preserve streamed output, validate locally, account exactly once."""

import json
import logging
from collections import Counter
from typing import Any

from graphiti_core.llm_client import LLMClient, LLMConfig
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, ModelSize
from graphiti_core.prompts import Message
from openai import AsyncOpenAI
from openai.lib._parsing._responses import type_to_text_format_param
from pydantic import BaseModel, ValidationError

from graph_service.openai_auth import RESOURCE, OpenAIAuth, connection_settings

logger = logging.getLogger(__name__)


class OAuthOutputError(ValueError):
    """Permanent extraction failure; neither LLM nor queue should retry it automatically."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f'OpenAI inference did not complete: {reason}')


def reject_json_constant(value: str) -> Any:
    # Python accepts NaN/Infinity by default; they are not valid provider JSON.
    raise OAuthOutputError('invalid_json')


class ResponseOutput:
    """One text per (output_index, content_index), not delta + done + terminal copies."""

    def __init__(self):
        self.deltas: dict[tuple[int, int], str] = {}
        self.complete: dict[tuple[int, int], str] = {}
        self.refusal = False
        self.conflict = False
        self.events: Counter[str] = Counter()

    def _text(self, key: tuple[int, int], text: str) -> None:
        if not text:
            return
        prior = self.complete.get(key)
        if prior is not None and prior != text:
            self.conflict = True
        self.complete[key] = text

    def item(self, output_index: int, item: Any) -> None:
        if item.type != 'message':
            return
        for index, part in enumerate(item.content):
            if part.type == 'refusal':
                self.refusal = True
            elif part.type == 'output_text':
                self._text((output_index, index), part.text)

    def accept(self, event: Any) -> None:
        kind = event.type
        self.events[kind] += 1
        if kind == 'response.output_text.delta':
            key = (event.output_index, event.content_index)
            self.deltas[key] = self.deltas.get(key, '') + event.delta
        elif kind == 'response.output_text.done':
            self._text((event.output_index, event.content_index), event.text)
        elif kind == 'response.output_item.done':
            self.item(event.output_index, event.item)
        elif kind in ('response.refusal.delta', 'response.refusal.done'):
            self.refusal = True
        elif kind == 'response.content_part.done':
            if event.part.type == 'refusal':
                self.refusal = True
            elif event.part.type == 'output_text':
                self._text((event.output_index, event.content_index), event.part.text)

    def text(self, response: Any) -> str:
        for index, item in enumerate(response.output or []):
            self.item(index, item)
        if self.refusal:
            raise OAuthOutputError('refusal')
        for key, delta in self.deltas.items():
            full = self.complete.get(key)
            # A canonical done/item may supply missing tail chunks, never concatenate copies.
            if full is not None and not full.startswith(delta):
                self.conflict = True
        if self.conflict:
            raise OAuthOutputError('conflicting_output')
        text = ''.join(
            self.complete.get(key, self.deltas.get(key, ''))
            for key in sorted(self.complete.keys() | self.deltas.keys())
        )
        if not text.strip():
            raise OAuthOutputError('empty_output')
        return text


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
        inputs = [
            {
                'role': 'developer' if m.role == 'system' else m.role,
                'content': self._clean_input(m.content),
            }
            for m in messages
        ]
        # SDK stream(text_format=Model) eagerly validates done/completed before we see usage.
        # Raw create(stream=True) keeps terminal usage and all output representations available.
        kwargs = {
            'model': model,
            'input': inputs,
            'store': False,
            'stream': True,
            'text': {
                'format': type_to_text_format_param(response_model)
                if response_model
                else {'type': 'json_object'}
            },
        }
        output = ResponseOutput()
        response = None
        outcome = 'request_failed'
        try:
            async with AsyncOpenAI(
                api_key=await self.auth.access_token(), base_url=RESOURCE, max_retries=0
            ) as client:
                stream = await client.responses.create(**kwargs)
                async with stream:
                    async for event in stream:
                        output.accept(event)
                        if event.type in (
                            'response.completed',
                            'response.failed',
                            'response.incomplete',
                        ):
                            if response is not None:
                                raise OAuthOutputError('multiple_terminal')
                            response = event.response
            if response is None:
                raise OAuthOutputError('missing_terminal')
            if response.status != 'completed':
                raise OAuthOutputError(response.status or 'unknown_status')
            text = output.text(response)
            try:
                parsed = json.loads(text, parse_constant=reject_json_constant)
            except (ValueError, TypeError):
                raise OAuthOutputError('invalid_json') from None
            if not isinstance(parsed, dict):
                raise OAuthOutputError('invalid_schema')
            if response_model:
                try:
                    parsed = response_model.model_validate(parsed).model_dump()
                except ValidationError:
                    # Pydantic errors contain input values; never propagate memory text to logs.
                    raise OAuthOutputError('invalid_schema') from None
            outcome = 'completed'
            return parsed
        except OAuthOutputError as exc:
            outcome = exc.reason
            raise
        finally:
            usage = response.usage if response is not None else None
            input_tokens = usage.input_tokens if usage else 0
            output_tokens = usage.output_tokens if usage else 0
            details = usage.input_tokens_details if usage else None
            cached_tokens = details.cached_tokens if details else 0
            self.auth.store.record_usage(
                'llm', model, input_tokens, output_tokens, cached_tokens, status=outcome
            )
            self.token_tracker.record(None, input_tokens, output_tokens)
            # Only counts/statuses: no prompts, output, refusal text, credentials, or SDK errors.
            terminal_items = response.output or [] if response is not None else []
            terminal_chars = sum(
                len(part.text)
                for item in terminal_items
                if item.type == 'message'
                for part in item.content
                if part.type == 'output_text'
            )
            logger.info(
                'OAuth response outcome=%s upstream=%s events=%s text_parts=%d '
                'delta_chars=%d canonical_chars=%d terminal_items=%d terminal_chars=%d',
                outcome,
                response.status if response is not None else 'none',
                dict(output.events),
                len(output.complete.keys() | output.deltas.keys()),
                sum(map(len, output.deltas.values())),
                sum(map(len, output.complete.values())),
                len(terminal_items),
                terminal_chars,
            )
