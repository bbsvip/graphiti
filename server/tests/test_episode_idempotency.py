import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from graphiti_core import Graphiti
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EpisodeType, EpisodicNode

from graph_service.mcp_runtime import upstream


def committed_episode(content='body'):
    return EpisodicNode(
        uuid='stable-id',
        group_id='diagnostic',
        name='test',
        source=EpisodeType.text,
        source_description='desc',
        content=content,
        valid_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_new_explicit_uuid_reaches_extraction_without_presaving(monkeypatch):
    from graphiti_core import graphiti as core

    graph = Graphiti.__new__(Graphiti)
    graph.tracer = MagicMock()
    driver = object()
    graph._resolve_request_scope = lambda group: (group, driver, object())
    graph.retrieve_episodes = AsyncMock(return_value=[])
    lookup = AsyncMock(side_effect=NodeNotFoundError('stable-id'))
    monkeypatch.setattr(EpisodicNode, 'get_by_uuid', lookup)
    extracted = []

    async def extract(clients, episode, *args):
        extracted.append(episode)
        raise ValueError('stop before provider call')

    monkeypatch.setattr(core, 'extract_nodes', extract)
    with pytest.raises(ValueError, match='stop before provider'):
        await graph.add_episode(
            'test',
            'body',
            'desc',
            datetime.now(timezone.utc),
            group_id='diagnostic',
            uuid='stable-id',
        )
    assert extracted[0].uuid == 'stable-id'
    assert extracted[0].content == 'body'


@pytest.mark.asyncio
async def test_queue_commit_before_ack_restart_does_not_extract_again(tmp_path, monkeypatch):
    graph = Graphiti.__new__(Graphiti)
    graph._resolve_request_scope = lambda group: (group, object(), object())
    graph.add_episode = AsyncMock()
    lookup = AsyncMock(side_effect=NodeNotFoundError('stable-id'))
    monkeypatch.setattr(EpisodicNode, 'get_by_uuid', lookup)
    path = tmp_path / 'queue.sqlite3'
    queue = upstream.QueueService(storage_path=path)
    await queue.initialize(graph)
    # Simulate the journal/graph gap deterministically without touching Neo4j.
    await queue.add_episode(
        group_id='diagnostic',
        name='test',
        content='body',
        source_description='desc',
        episode_type='text',
        entity_types=None,
        uuid='stable-id',
    )
    await queue.shutdown()
    queue._store.update('stable-id', 'processing')
    monkeypatch.setattr(
        EpisodicNode,
        'get_by_uuid',
        AsyncMock(return_value=committed_episode()),
    )
    graph.add_episode.reset_mock()
    recovered = upstream.QueueService(storage_path=path)
    await recovered.initialize(graph)
    await recovered.shutdown()
    assert recovered.get_job('stable-id', 'diagnostic')['status'] == 'succeeded'
    graph.add_episode.assert_not_awaited()


@pytest.mark.asyncio
async def test_committed_episode_with_optional_post_commit_work_is_not_false_success(
    tmp_path, monkeypatch
):
    graph = Graphiti.__new__(Graphiti)
    graph._resolve_request_scope = lambda group: (group, object(), object())
    graph.add_episode = AsyncMock()
    monkeypatch.setattr(EpisodicNode, 'get_by_uuid', AsyncMock(return_value=committed_episode()))
    queue = upstream.QueueService(storage_path=tmp_path / 'q.sqlite3')
    await queue.initialize(graph)
    await queue.add_episode(
        group_id='diagnostic',
        name='test',
        content='body',
        source_description='desc',
        episode_type='text',
        entity_types=None,
        uuid='stable-id',
        update_communities=True,
    )
    await queue.shutdown()
    status = queue.get_job('stable-id', 'diagnostic')
    assert status['status'] == 'failed'
    assert status['error'] == 'PostCommitRecoveryRequired'
    graph.add_episode.assert_not_awaited()


@pytest.mark.asyncio
async def test_graph_readback_preflight_has_processing_status(tmp_path, monkeypatch):
    graph = Graphiti.__new__(Graphiti)
    graph._resolve_request_scope = lambda group: (group, object(), object())
    graph.add_episode = AsyncMock()
    started = asyncio.Event()

    async def blocked_read(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(EpisodicNode, 'get_by_uuid', blocked_read)
    queue = upstream.QueueService(storage_path=tmp_path / 'q.sqlite3')
    await queue.initialize(graph)
    await queue.add_episode(
        group_id='diagnostic',
        name='test',
        content='body',
        source_description='desc',
        episode_type='text',
        entity_types=None,
        uuid='stable-id',
    )
    await started.wait()
    try:
        assert queue.get_job('stable-id', 'diagnostic')['status'] == 'processing'
    finally:
        await queue.shutdown(timeout=0.01)
    assert queue.get_job('stable-id', 'diagnostic')['status'] == 'queued'


@pytest.mark.asyncio
async def test_existing_graph_uuid_with_different_content_is_not_success(tmp_path, monkeypatch):
    graph = Graphiti.__new__(Graphiti)
    graph._resolve_request_scope = lambda group: (group, object(), object())
    graph.store_raw_episode_content = True
    graph.add_episode = AsyncMock()
    monkeypatch.setattr(
        EpisodicNode, 'get_by_uuid', AsyncMock(return_value=committed_episode('different body'))
    )
    queue = upstream.QueueService(storage_path=tmp_path / 'q.sqlite3')
    await queue.initialize(graph)
    await queue.add_episode(
        group_id='diagnostic',
        name='test',
        content='body',
        source_description='desc',
        episode_type='text',
        entity_types=None,
        uuid='stable-id',
    )
    await queue.shutdown()
    assert queue.get_job('stable-id', 'diagnostic')['status'] == 'failed'
    graph.add_episode.assert_not_awaited()
