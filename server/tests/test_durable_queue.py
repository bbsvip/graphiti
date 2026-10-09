import asyncio
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import Timeout
from pydantic import BaseModel

from graph_service.mcp_runtime import upstream


class Attribute(BaseModel):
    value: str


class Writer:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.started = asyncio.Event()
        self.block = False

    async def add_episode(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        if self.failure:
            raise self.failure
        return SimpleNamespace(episode=SimpleNamespace(uuid=kwargs['uuid']))


async def enqueue(queue, name, uuid=None, **kwargs):
    return await queue.add_episode(
        group_id='diagnostic',
        name=name,
        content='private-body',
        source_description='test',
        episode_type='text',
        entity_types=None,
        uuid=uuid,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_two_episodes_and_failed_payload_survive_restart(tmp_path):
    path = tmp_path / 'queue.sqlite3'
    queue = upstream.QueueService(storage_path=path, retry_delay=0)
    writer = Writer()
    writer.failure = ValueError('invalid_schema private-body')
    await queue.initialize(writer)
    first = await enqueue(queue, 'first', 'first-id')
    await queue._episode_queues['diagnostic'].join()
    assert first['status'] == 'queued'
    assert queue.get_job('first-id', 'diagnostic')['status'] == 'failed'
    assert queue.get_job('first-id', 'diagnostic')['attempts'] == 1
    writer.failure = None
    second = await enqueue(queue, 'second', 'second-id')
    await queue.shutdown()
    assert second['status'] == 'queued'
    assert [call['name'] for call in writer.calls] == ['first', 'second']
    recovered = upstream.QueueService(storage_path=path, retry_delay=0)
    await recovered.initialize(Writer())
    assert recovered.get_job('first-id', 'diagnostic')['status'] == 'failed'
    assert recovered.get_job('second-id', 'diagnostic')['status'] == 'succeeded'
    assert 'private-body' not in str(recovered.get_job('first-id', 'diagnostic'))
    await recovered.shutdown()


@pytest.mark.asyncio
async def test_cancel_then_restart_preserves_uuid_time_order_and_config(tmp_path):
    path = tmp_path / 'queue.sqlite3'
    writer = Writer()
    writer.block = True
    queue = upstream.QueueService(storage_path=path, retry_delay=0)
    await queue.initialize(writer, entity_types={'Attribute': Attribute})
    ref = datetime(2024, 1, 1, tzinfo=timezone.utc)
    first = await queue.add_episode(
        group_id='diagnostic',
        name='first',
        content='body',
        source_description='test',
        episode_type='text',
        entity_types={'Attribute': Attribute},
        uuid=None,
        reference_time=ref,
    )
    await writer.started.wait()
    await enqueue(queue, 'second', 'second-id')
    assert queue.get_job(first['uuid'], 'diagnostic')['status'] == 'processing'
    await queue.shutdown(timeout=0.01)
    recovered_writer = Writer()
    recovered = upstream.QueueService(storage_path=path, retry_delay=0)
    await recovered.initialize(recovered_writer, entity_types={'Attribute': Attribute})
    await recovered.shutdown()
    assert [call['name'] for call in recovered_writer.calls] == ['first', 'second']
    assert recovered_writer.calls[0]['uuid'] == first['uuid'] == writer.calls[0]['uuid']
    assert recovered_writer.calls[0]['reference_time'] == ref
    assert recovered_writer.calls[0]['entity_types']['Attribute'] is Attribute
    assert recovered.get_job(first['uuid'], 'diagnostic')['status'] == 'succeeded'


@pytest.mark.asyncio
async def test_retry_is_bounded_and_same_uuid_does_not_enqueue_again(tmp_path):
    writer = Writer()
    writer.failure = TimeoutError('temporary')
    queue = upstream.QueueService(
        storage_path=tmp_path / 'q.sqlite3', max_attempts=2, retry_delay=0
    )
    await queue.initialize(writer)
    await enqueue(queue, 'first', 'stable-id')
    await queue._episode_queues['diagnostic'].join()
    assert len(writer.calls) == 2
    assert {call['uuid'] for call in writer.calls} == {'stable-id'}
    assert queue.get_job('stable-id', 'diagnostic')['status'] == 'failed'
    duplicate = await enqueue(queue, 'first', 'stable-id')
    assert duplicate['status'] == 'failed'
    assert len(writer.calls) == 2
    with pytest.raises(ValueError, match='conflict'):
        await enqueue(queue, 'different', 'stable-id')
    await queue.shutdown()


@pytest.mark.asyncio
async def test_only_one_owner_and_admission_stops_during_shutdown(tmp_path):
    path = tmp_path / 'q.sqlite3'
    queue = upstream.QueueService(storage_path=path)
    await queue.initialize(Writer())
    other = upstream.QueueService(storage_path=path)
    with pytest.raises(Timeout):
        await other.initialize(Writer())
    task = asyncio.create_task(queue.shutdown())
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match='shutting down'):
        await enqueue(queue, 'too-late')
    await task


@pytest.mark.asyncio
async def test_explicit_retry_recovers_failed_payload_with_same_uuid(tmp_path):
    writer = Writer()
    writer.failure = ValueError('invalid_schema')
    queue = upstream.QueueService(storage_path=tmp_path / 'q.sqlite3', max_attempts=2)
    await queue.initialize(writer)
    await enqueue(queue, 'first', 'stable-id')
    await queue._episode_queues['diagnostic'].join()
    writer.failure = None
    await queue.retry_episode('stable-id', 'diagnostic')
    await queue._episode_queues['diagnostic'].join()
    assert queue.get_job('stable-id', 'diagnostic')['status'] == 'succeeded'
    assert len(writer.calls) == 2
    assert writer.calls[0] == writer.calls[1]
    await queue.shutdown()


@pytest.mark.asyncio
async def test_abrupt_process_exit_recovers_two_jobs_in_order(tmp_path):
    path = tmp_path / 'crash.sqlite3'
    source = Path(__file__).resolve().parents[2] / 'mcp_server' / 'src'
    program = """
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from services.queue_service import QueueService
started = asyncio.Event()
class Writer:
    async def add_episode(self, **kwargs):
        started.set()
        await asyncio.Event().wait()
async def main():
    queue = QueueService(storage_path=Path(sys.argv[2]))
    await queue.initialize(Writer())
    for name in ('first', 'second'):
        await queue.add_episode(group_id='diagnostic', name=name, content='body',
            source_description='test', episode_type='text', entity_types=None, uuid=name+'-id')
    await started.wait()
    os._exit(0)
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, '-c', program, str(source), str(path)], capture_output=True, timeout=20
    )
    assert result.returncode == 0, result.stderr.decode()
    writer = Writer()
    queue = upstream.QueueService(storage_path=path)
    await queue.initialize(writer)
    await queue.shutdown()
    assert [entry['name'] for entry in writer.calls] == ['first', 'second']
    assert [
        queue.get_job(name + '-id', 'diagnostic')['status'] for name in ('first', 'second')
    ] == ['succeeded', 'succeeded']
