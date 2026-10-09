"""Exercise the installed SDK transport, not an invented response.output_text mock."""

import json
import logging

import httpx
import pytest
from graphiti_core.prompts import Message
from openai import AsyncOpenAI
from pydantic import BaseModel

from graph_service import oauth_client
from graph_service.openai_auth import OpenAIAuth, RuntimeStore


class Extracted(BaseModel):
    answer: str


def response(status='completed', text=None):
    return {
        'id': 'resp_test',
        'created_at': 1,
        'object': 'response',
        'model': 'gpt-5.5',
        'status': status,
        'error': None,
        'incomplete_details': None,
        'instructions': None,
        'parallel_tool_calls': True,
        'tool_choice': 'auto',
        'tools': [],
        'output': [] if text is None else [item(text)],
        'usage': {
            'input_tokens': 10,
            'output_tokens': 2,
            'total_tokens': 12,
            'input_tokens_details': {'cached_tokens': 3},
            'output_tokens_details': {'reasoning_tokens': 0},
        },
    }


def item(text):
    return {
        'type': 'message',
        'id': 'msg_test',
        'status': 'completed',
        'role': 'assistant',
        'content': [{'type': 'output_text', 'text': text, 'annotations': []}],
    }


def stream_events(text='{"answer":"yes"}', terminal_text=None, status='completed', terminal=True):
    events = [
        {'type': 'response.created', 'response': response('in_progress')},
        {
            'type': 'response.output_item.added',
            'output_index': 0,
            'item': {**item(''), 'content': [], 'status': 'in_progress'},
        },
        {
            'type': 'response.content_part.added',
            'output_index': 0,
            'content_index': 0,
            'item_id': 'msg_test',
            'part': {'type': 'output_text', 'text': '', 'annotations': []},
        },
    ]
    # Three chunks, one canonical done text: concatenating every representation corrupts JSON.
    for chunk in (text[:4], text[4:9], text[9:]):
        events.append(
            {
                'type': 'response.output_text.delta',
                'output_index': 0,
                'content_index': 0,
                'item_id': 'msg_test',
                'delta': chunk,
            }
        )
    events += [
        {
            'type': 'response.output_text.done',
            'output_index': 0,
            'content_index': 0,
            'item_id': 'msg_test',
            'text': text,
        },
        {'type': 'response.output_item.done', 'output_index': 0, 'item': item(text)},
    ]
    if terminal:
        events.append({'type': 'response.' + status, 'response': response(status, terminal_text)})
    return [{**event, 'sequence_number': i} for i, event in enumerate(events)]


def adapter(tmp_path, monkeypatch, events):
    auth = OpenAIAuth(RuntimeStore(tmp_path), 8123)

    async def token():
        return 'test-oauth-token'

    def handle(request):
        payload = json.loads(request.content)
        assert payload['model'] == 'gpt-5.5'
        assert payload['text']['format']['type'] == 'json_schema'
        assert payload['text']['format']['strict'] is True
        assert payload['input'][0]['role'] == 'developer'
        assert 'max_output_tokens' not in payload
        content = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=content)

    def client(**kwargs):
        return AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )

    monkeypatch.setattr(auth, 'access_token', token)
    monkeypatch.setattr(oauth_client, 'AsyncOpenAI', client)
    monkeypatch.setattr(oauth_client, 'connection_settings', lambda: {'model': 'gpt-5.5'})
    return oauth_client.OpenAIOAuthClient(auth), auth.store


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal_text', [None, '{"answer":"yes"}'])
async def test_chunks_survive_empty_terminal_without_duplication(
    tmp_path, monkeypatch, terminal_text
):
    llm, store = adapter(tmp_path, monkeypatch, stream_events(terminal_text=terminal_text))
    assert await llm._generate_response(
        [Message(role='system', content='private-memory')], Extracted
    ) == {'answer': 'yes'}
    totals = store.usage_summary()['totals']
    assert (totals['requests'], totals['failures'], totals['total_tokens']) == (1, 0, 12)
    assert llm.token_tracker.get_total_usage().input_tokens == 10


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'events,reason,tokens',
    [
        (stream_events(status='incomplete'), 'incomplete', 12),
        (stream_events(status='failed'), 'failed', 12),
        (stream_events(terminal=False), 'missing_terminal', 0),
        (stream_events(text=''), 'empty_output', 12),
        (stream_events(text='not JSON'), 'invalid_json', 12),
        (stream_events(text='{"answer":NaN}'), 'invalid_json', 12),
        (stream_events(text='{"wrong":1}'), 'invalid_schema', 12),
        (stream_events(terminal_text='{"answer":"different"}'), 'conflicting_output', 12),
    ],
)
async def test_output_failure_accounted_once_with_terminal_usage(
    tmp_path, monkeypatch, events, reason, tokens, caplog
):
    caplog.set_level(logging.INFO, logger='graph_service.oauth_client')
    llm, store = adapter(tmp_path, monkeypatch, events)
    with pytest.raises(ValueError, match=reason):
        await llm._generate_response([Message(role='system', content='private-memory')], Extracted)
    totals = store.usage_summary()['totals']
    assert (totals['requests'], totals['failures'], totals['total_tokens']) == (1, 1, tokens)
    assert 'private-memory' not in caplog.text
    assert 'test-oauth-token' not in caplog.text
    assert f'outcome={reason}' in caplog.text


@pytest.mark.asyncio
async def test_refusal_is_not_empty_output(tmp_path, monkeypatch):
    terminal = response()
    terminal['output'] = [
        {**item(''), 'content': [{'type': 'refusal', 'refusal': 'private refusal'}]}
    ]
    events = [
        {'type': 'response.created', 'response': response('in_progress'), 'sequence_number': 0},
        {'type': 'response.completed', 'response': terminal, 'sequence_number': 1},
    ]
    llm, store = adapter(tmp_path, monkeypatch, events)
    with pytest.raises(ValueError, match='refusal'):
        await llm._generate_response([Message(role='system', content='private-memory')], Extracted)
    assert store.usage_summary()['totals']['failures'] == 1


@pytest.mark.asyncio
async def test_installed_sdk_final_response_does_not_rebuild_empty_terminal_output():
    events = stream_events()
    encoded = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={'content-type': 'text/event-stream'}, content=encoded
        )
    )
    async with (
        AsyncOpenAI(
            api_key='test-only', http_client=httpx.AsyncClient(transport=transport)
        ) as client,
        client.responses.stream(model='gpt-5.5', input='test', text_format=Extracted) as stream,
    ):
        texts = [event.text async for event in stream if event.type == 'response.output_text.done']
        final = await stream.get_final_response()
    assert texts == ['{"answer":"yes"}']
    assert final.output_text == ''
