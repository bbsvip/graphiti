"""Ordered episode workers with a durable journal and bounded, classified retry."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from filelock import FileLock
from graphiti_core import Graphiti
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EpisodeType, EpisodicNode
from openai import APIConnectionError, APIStatusError

from services.episode_store import EpisodeStore

logger = logging.getLogger(__name__)


def transient(error: Exception) -> bool:
    # ValueError includes refusal/output/schema errors: do not spend tokens retrying them.
    if isinstance(error, APIStatusError | httpx.HTTPStatusError):
        status = (
            error.status_code if isinstance(error, APIStatusError) else error.response.status_code
        )
        return status == 429 or status >= 500
    return isinstance(
        error, TimeoutError | ConnectionError | APIConnectionError | httpx.TransportError
    )


class PostCommitRecoveryRequired(ValueError):
    """Extraction committed, but optional saga/community effects need operator reconciliation."""


class QueueService:
    """Single owner; one sequential worker per group. Failed jobs remain inspectable."""

    def __init__(
        self, storage_path: Path | None = None, max_attempts: int = 3, retry_delay: float = 1
    ):
        if max_attempts < 1:
            raise ValueError('max_attempts must be positive')
        self._store = EpisodeStore(storage_path)
        self._lock = FileLock(str(storage_path) + '.lock') if storage_path else None
        self._episode_queues: dict[str, asyncio.Queue] = {}
        self._queue_workers: dict[str, bool] = {}
        self._worker_tasks: dict[str, asyncio.Task] = {}
        self._graphiti_client: Any = None
        self._entity_types: dict = {}
        self._edge_types: dict = {}
        self._closing = False
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay

    async def add_episode_task(
        self, group_id: str, process_func: Callable[[], Awaitable[None]] | str
    ) -> int:
        """Compatibility for ephemeral callbacks, NOT the durable episode submission API."""
        if self._closing:
            raise RuntimeError('Queue is shutting down')
        queue = self._episode_queues.setdefault(group_id, asyncio.Queue())
        await queue.put(process_func)
        if not self._queue_workers.get(group_id, False):
            self._queue_workers[group_id] = True
            self._worker_tasks[group_id] = asyncio.create_task(
                self._process_episode_queue(group_id)
            )
        return queue.qsize()

    async def shutdown(self, timeout: float = 5) -> None:
        self._closing = True
        try:
            await asyncio.wait_for(
                asyncio.gather(*(q.join() for q in self._episode_queues.values())), timeout
            )
        except asyncio.TimeoutError:
            logger.warning('Episode queue drain timed out; journaled work retained for restart')
        finally:
            for task in self._worker_tasks.values():
                task.cancel()
            await asyncio.gather(*self._worker_tasks.values(), return_exceptions=True)
            self._worker_tasks.clear()
            self._episode_queues.clear()
            self._queue_workers.clear()
            if self._lock and self._lock.is_locked:
                self._lock.release()
            # Legacy ephemeral callback tests may reuse an uninitialized queue after draining.
            if self._graphiti_client is None:
                self._closing = False

    async def _process_episode_queue(self, group_id: str) -> None:
        queue = self._episode_queues[group_id]
        try:
            while True:
                work = await queue.get()
                try:
                    if isinstance(work, str):
                        await self._process_job(work)
                    else:
                        await work()
                except asyncio.CancelledError:
                    if isinstance(work, str):
                        self._store.update(work, 'queued')
                    raise
                except Exception as error:
                    # Never stringify exception values: they can contain the episode or credentials.
                    logger.error('Episode worker error type=%s', type(error).__name__)
                    if isinstance(work, str):
                        self._store.update(work, 'failed', type(error).__name__)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            pass
        finally:
            self._queue_workers[group_id] = False

    async def _already_committed(self, payload: dict) -> bool:
        client = self._graphiti_client
        if not issubclass(type(client), Graphiti):
            return False
        _, driver, _ = client._resolve_request_scope(payload['group_id'])
        try:
            episode = await EpisodicNode.get_by_uuid(driver, payload['uuid'])
        except NodeNotFoundError:
            return False
        if (
            episode.group_id != payload['group_id']
            or episode.name != payload['name']
            or episode.source.value != payload['episode_type']
            or episode.source_description != payload['source_description']
            or (
                getattr(client, 'store_raw_episode_content', True)
                and episode.content != payload['content']
            )
        ):
            raise ValueError('Episode UUID graph conflict')
        if payload.get('saga') or payload.get('update_communities'):
            # These phases run AFTER the extraction transaction. Presence alone does not
            # prove they completed; do not re-extract or report an unverified success.
            raise PostCommitRecoveryRequired()
        # add_episode saves the episodic node together with extracted nodes/edges in one
        # transaction, never before extraction. A readback covers commit-before-ack crashes.
        return True

    def _restore_types(self, saved: dict | None, registry: dict) -> dict | None:
        if saved is None:
            return None
        result = {}
        for name, schema in saved.items():
            model = registry.get(name)
            if model is None or model.model_json_schema() != schema:
                raise ValueError('Configured extraction schema changed; manual recovery required')
            result[name] = model
        return result

    async def _process_job(self, uuid: str) -> None:
        while True:
            row = self._store.require(uuid)
            payload = json.loads(row['payload'])
            self._store.update(uuid, 'processing')
            try:
                if await self._already_committed(payload):
                    self._store.update(uuid, 'succeeded')
                    return
                if row['attempts'] >= self.max_attempts:
                    self._store.update(uuid, 'failed', 'retry_exhausted')
                    return
                self._store.update(uuid, 'processing', attempt=True)
                kwargs = {**payload}
                kwargs['source'] = EpisodeType(kwargs.pop('episode_type'))
                kwargs['episode_body'] = kwargs.pop('content')
                kwargs['reference_time'] = datetime.fromisoformat(kwargs['reference_time'])
                kwargs['entity_types'] = self._restore_types(
                    kwargs['entity_types'], self._entity_types
                )
                kwargs['edge_types'] = self._restore_types(kwargs['edge_types'], self._edge_types)
                edge_map = kwargs['edge_type_map']
                kwargs['edge_type_map'] = (
                    {tuple(pair): names for pair, names in edge_map}
                    if edge_map is not None
                    else None
                )
                await self._graphiti_client.add_episode(**kwargs)
                self._store.update(uuid, 'succeeded')
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                attempts = self._store.require(uuid)['attempts']
                # Count preflight transport failures too; otherwise readback retries can loop forever.
                if attempts == row['attempts']:
                    self._store.update(uuid, 'processing', attempt=True)
                    attempts += 1
                category = getattr(error, 'reason', type(error).__name__)
                if not transient(error) or attempts >= self.max_attempts:
                    self._store.update(uuid, 'failed', category)
                    logger.warning(
                        'Episode job failed uuid=%s error_type=%s attempts=%d',
                        uuid,
                        type(error).__name__,
                        attempts,
                    )
                    return
                self._store.update(uuid, 'queued', category)
                await asyncio.sleep(self.retry_delay * attempts)

    async def retry_episode(self, uuid: str, group_id: str) -> dict:
        """Operator-requested retry only; retains identity, payload and lifetime attempt budget."""
        if self._closing:
            raise RuntimeError('Queue is shutting down')
        job = self.get_job(uuid, group_id)
        if job['status'] != 'failed':
            return job
        if job['attempts'] >= self.max_attempts:
            raise ValueError('Retry budget exhausted; manual operator recovery required')
        self._store.update(uuid, 'queued')
        await self.add_episode_task(group_id, uuid)
        return self.get_job(uuid, group_id)

    def get_job(self, uuid: str, group_id: str) -> dict:
        return self._store.status(uuid, group_id)

    def get_queue_size(self, group_id: str) -> int:
        queue = self._episode_queues.get(group_id)
        return queue.qsize() if queue else 0

    def is_worker_running(self, group_id: str) -> bool:
        return self._queue_workers.get(group_id, False)

    async def initialize(
        self, graphiti_client: Any, entity_types: dict | None = None, edge_types: dict | None = None
    ) -> None:
        if self._lock:
            self._lock.acquire(timeout=0)
        self._graphiti_client = graphiti_client
        self._entity_types = entity_types or {}
        self._edge_types = edge_types or {}
        self._closing = False
        for row in self._store.recover():
            await self.add_episode_task(row['group_id'], row['uuid'])

    async def add_episode(
        self,
        group_id: str,
        name: str,
        content: str,
        source_description: str,
        episode_type: Any,
        entity_types: Any,
        uuid: str | None,
        reference_time: datetime | None = None,
        edge_types: Any = None,
        edge_type_map: Any = None,
        excluded_entity_types: list[str] | None = None,
        previous_episode_uuids: list[str] | None = None,
        custom_extraction_instructions: str | None = None,
        update_communities: bool = False,
        saga: str | None = None,
        saga_previous_episode_uuid: str | None = None,
    ) -> dict:
        if self._closing:
            raise RuntimeError('Queue is shutting down')
        if self._graphiti_client is None:
            raise RuntimeError('Queue service not initialized. Call initialize() first.')
        payload: dict[str, Any] = dict(
            group_id=group_id,
            name=name,
            content=content,
            source_description=source_description,
            episode_type=episode_type.value
            if isinstance(episode_type, EpisodeType)
            else episode_type,
            entity_types={k: v.model_json_schema() for k, v in entity_types.items()}
            if entity_types is not None
            else None,
            edge_types={k: v.model_json_schema() for k, v in edge_types.items()}
            if edge_types is not None
            else None,
            edge_type_map=sorted([[list(pair), names] for pair, names in edge_type_map.items()])
            if edge_type_map is not None
            else None,
            uuid=uuid or str(uuid4()),
            reference_time=(reference_time or datetime.now(timezone.utc)).isoformat(),
            excluded_entity_types=excluded_entity_types,
            previous_episode_uuids=previous_episode_uuids,
            custom_extraction_instructions=custom_extraction_instructions,
            update_communities=update_communities,
            saga=saga,
            saga_previous_episode_uuid=saga_previous_episode_uuid,
        )
        self._entity_types.update(entity_types or {})
        self._edge_types.update(edge_types or {})
        row, created = self._store.submit(payload, implicit_time=reference_time is None)
        if created:
            await self.add_episode_task(group_id, payload['uuid'])
        return {key: row[key] for key in ('uuid', 'group_id', 'status', 'attempts', 'error')}
