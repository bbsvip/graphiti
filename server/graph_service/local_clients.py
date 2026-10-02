"""Keyless adapters for the self-hosted Infinity service."""

import math
from collections.abc import Iterable

import httpx
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder import EmbedderClient
from graphiti_core.embedder.client import EmbedderConfig

from graph_service.openai_auth import RuntimeStore


class InfinityEmbedder(EmbedderClient):
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        model: str,
        dimensions: int,
        store: RuntimeStore,
    ):
        self.http = http
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.config = EmbedderConfig(embedding_dim=dimensions)
        self.store = store

    async def create(
        self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]
    ) -> list[float]:
        if isinstance(input_data, str):
            inputs = [input_data]
        else:
            inputs = list(input_data)
            if not all(isinstance(value, str) for value in inputs):
                raise ValueError('Infinity text embeddings require strings, not token IDs')
        vectors = await self.create_batch(inputs)  # type: ignore[arg-type]
        if not vectors:
            raise ValueError('Cannot embed an empty input')
        return vectors[0]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        # Infinity's schema caps each text batch at 2048, without deduplication.
        for start in range(0, len(input_data_list), 2048):
            inputs = input_data_list[start : start + 2048]
            response = await self.http.post(
                self.base_url + '/embeddings',
                json={
                    'model': self.model,
                    'input': inputs,
                    'encoding_format': 'float',
                    'dimensions': self.config.embedding_dim,
                },
            )
            response.raise_for_status()
            body = response.json()
            usage = body.get('usage', {})
            self.store.record_usage('embedding', self.model, usage.get('prompt_tokens', 0))
            data = sorted(body['data'], key=lambda item: item['index'])
            if [item['index'] for item in data] != list(range(len(inputs))):
                raise ValueError('Infinity returned missing or duplicate embedding indices')
            batch = [item['embedding'] for item in data]
            if any(len(vector) != self.config.embedding_dim for vector in batch):
                raise ValueError(
                    'Infinity embedding dimension differs from the graph configuration'
                )
            if any(not math.isfinite(value) for vector in batch for value in vector):
                raise ValueError('Infinity returned non-finite embeddings')
            vectors.extend(batch)
        return vectors


class InfinityReranker(CrossEncoderClient):
    def __init__(self, http: httpx.AsyncClient, base_url: str, model: str, store: RuntimeStore):
        self.http = http
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.store = store

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        if not passages:
            return []
        if not self.model:
            raise ValueError('Choose an Infinity reranker model at /admin first')
        response = await self.http.post(
            self.base_url + '/rerank',
            json={
                'model': self.model,
                'query': query,
                'documents': passages,
                'top_n': len(passages),
                'return_documents': False,
            },
        )
        response.raise_for_status()
        body = response.json()
        self.store.record_usage(
            'reranker', self.model, body.get('usage', {}).get('prompt_tokens', 0)
        )
        results = body['results']
        if sorted(item['index'] for item in results) != list(range(len(passages))):
            raise ValueError('Infinity returned missing or duplicate reranker indices')
        scores = [(passages[item['index']], float(item['relevance_score'])) for item in results]
        if any(not math.isfinite(score) for _, score in scores):
            raise ValueError('Infinity returned non-finite reranker scores')
        return sorted(scores, key=lambda item: item[1], reverse=True)
