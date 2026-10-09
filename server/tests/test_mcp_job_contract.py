from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from graph_service.mcp_runtime import upstream


@pytest.mark.asyncio
async def test_add_memory_returns_job_identity_and_status_not_persistence(tmp_path, monkeypatch):
    queue = upstream.QueueService(storage_path=tmp_path / 'q.sqlite3')
    await queue.initialize(SimpleNamespace(add_episode=AsyncMock()))
    monkeypatch.setattr(upstream, 'queue_service', queue)
    monkeypatch.setattr(
        upstream,
        'graphiti_service',
        SimpleNamespace(entity_types=None, edge_types=None, edge_type_map=None),
    )
    monkeypatch.setattr(
        upstream,
        'config',
        SimpleNamespace(graphiti=SimpleNamespace(group_id='diagnostic')),
        raising=False,
    )
    result = await upstream.add_memory(name='small-test', episode_body='body', uuid='stable-id')
    assert result['uuid'] == 'stable-id'
    assert result['status'] == 'queued'
    assert 'not yet stored' in result['message']
    await queue.shutdown()
    status = await upstream.get_episode_status(uuid='stable-id', group_id='diagnostic')
    assert status['status'] == 'succeeded'
    assert status['attempts'] == 1
    # Admission duplicate returns actual persisted state, not invented "queued".
    duplicate = await upstream.add_memory(name='small-test', episode_body='body', uuid='stable-id')
    assert 'error' in duplicate  # closed queues must refuse admission
    unknown = await upstream.get_episode_status(uuid='missing', group_id='diagnostic')
    assert 'error' in unknown


@pytest.mark.asyncio
async def test_manual_mcp_retry_is_explicit_and_budgeted(tmp_path, monkeypatch):
    writer = SimpleNamespace(add_episode=AsyncMock(side_effect=ValueError('invalid_schema')))
    queue = upstream.QueueService(storage_path=tmp_path / 'q.sqlite3', max_attempts=2)
    await queue.initialize(writer)
    monkeypatch.setattr(upstream, 'queue_service', queue)
    await queue.add_episode(
        group_id='diagnostic',
        name='test',
        content='body',
        source_description='desc',
        episode_type='text',
        entity_types=None,
        uuid='stable-id',
    )
    await queue._episode_queues['diagnostic'].join()
    result = await upstream.retry_episode(uuid='stable-id', group_id='diagnostic')
    assert result['status'] == 'queued'
    await queue._episode_queues['diagnostic'].join()
    assert queue.get_job('stable-id', 'diagnostic')['attempts'] == 2
    exhausted = await upstream.retry_episode(uuid='stable-id', group_id='diagnostic')
    assert 'error' in exhausted
    assert writer.add_episode.await_count == 2
    await queue.shutdown()


@pytest.mark.asyncio
async def test_standalone_transport_drains_queue_before_client_close(tmp_path, monkeypatch):
    writer = SimpleNamespace(add_episode=AsyncMock(), close=AsyncMock())
    queue = upstream.QueueService(storage_path=tmp_path / 'q.sqlite3')
    await queue.initialize(writer)

    async def close_after_drain():
        assert queue.get_job('second-id', 'diagnostic')['status'] == 'succeeded'
        assert not queue._worker_tasks

    writer.close = AsyncMock(side_effect=close_after_drain)
    monkeypatch.setattr(upstream, 'queue_service', queue)
    monkeypatch.setattr(upstream, 'graphiti_client', writer)
    monkeypatch.setattr(
        upstream, 'initialize_server', AsyncMock(return_value=SimpleNamespace(transport='stdio'))
    )
    monkeypatch.setattr(upstream.mcp, 'run_stdio_async', AsyncMock())
    for name in ('first', 'second'):
        await queue.add_episode(
            group_id='diagnostic',
            name=name,
            content='body',
            source_description='desc',
            episode_type='text',
            entity_types=None,
            uuid=name + '-id',
        )
    try:
        await upstream.run_mcp_server()
        writer.close.assert_awaited_once()
        assert not queue._worker_tasks
        assert queue.get_job('second-id', 'diagnostic')['status'] == 'succeeded'
    finally:
        await queue.shutdown()
