"""Opt-in deployment gate: one stable diagnostic episode, then direct Neo4j readback.

Never run this before a verified rebuild. Re-running with the same UUID polls the
existing job, not a new write. Failed/ambiguous admission requires operator review.
"""

import asyncio
import os
import time
from uuid import UUID

import httpx
import pytest
from neo4j import AsyncGraphDatabase

BODY = 'MCPProbeLinh works for MCPProbeLab.'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deployed_mcp_persists_episode_entities_and_fact_in_neo4j():
    if os.environ.get('GRAPHITI_VERIFY_MCP_INGEST') != '1':
        pytest.skip('Explicit deployment verification permission required')
    uuid = str(UUID(os.environ['GRAPHITI_VERIFY_EPISODE_UUID']))
    group = os.environ.get('GRAPHITI_VERIFY_GROUP', 'mcp_diag_ingest')
    assert group.startswith('mcp_diag_'), 'Only a dedicated diagnostic group is allowed'
    password = os.environ['NEO4J_PASSWORD']
    session = None
    identifier = 0
    async with httpx.AsyncClient(timeout=30) as http:
        url = os.environ.get('GRAPHITI_MCP_URL', 'http://192.168.1.11:8123/mcp/')

        async def rpc(method, params):
            nonlocal session, identifier
            identifier += 1
            response = await http.post(
                url,
                headers={
                    'accept': 'application/json, text/event-stream',
                    **({'mcp-session-id': session} if session else {}),
                },
                json={'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params},
            )
            response.raise_for_status()
            if method == 'initialize':
                session = response.headers['mcp-session-id']
            body = response.json()
            assert 'error' not in body, 'MCP JSON-RPC error; reconcile UUID before any retry'
            assert 'result' in body
            result = body['result']
            assert not result.get('isError'), 'MCP tool error; do not retry write blindly'
            payload = result.get('structuredContent', {})
            if 'result' in payload:
                payload = payload['result']
            return payload or result

        await rpc(
            'initialize',
            {
                'protocolVersion': '2025-03-26',
                'capabilities': {},
                'clientInfo': {'name': 'graphiti-readback-gate', 'version': '1'},
            },
        )
        await http.post(
            url,
            headers={'accept': 'application/json, text/event-stream', 'mcp-session-id': session},
            json={'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        )
        args = {'uuid': uuid, 'group_id': group}
        status = await rpc('tools/call', {'name': 'get_episode_status', 'arguments': args})
        if status.get('error') == 'Episode job not found':
            status = await rpc(
                'tools/call',
                {
                    'name': 'add_memory',
                    'arguments': {
                        **args,
                        'name': 'MCP readback diagnostic',
                        'episode_body': BODY,
                        'source': 'text',
                        'source_description': 'isolated deployment diagnostic',
                    },
                },
            )
        deadline = time.monotonic() + 300
        while status.get('status') in ('queued', 'processing') and time.monotonic() < deadline:
            await asyncio.sleep(1)
            status = await rpc('tools/call', {'name': 'get_episode_status', 'arguments': args})
        assert status.get('status') == 'succeeded', f'Diagnostic job did not succeed: {status}'

    async with (
        AsyncGraphDatabase.driver(
            os.environ.get('NEO4J_URI', 'bolt://192.168.1.11:7687'),
            auth=(os.environ.get('NEO4J_USER', 'neo4j'), password),
        ) as driver,
        driver.session(database=os.environ.get('NEO4J_DATABASE', 'neo4j')) as db,
    ):
        result = await db.run(
            """MATCH (e:Episodic {uuid:$uuid, group_id:$group})
                OPTIONAL MATCH (e)-[:MENTIONS]->(n:Entity)
                RETURN e.content AS content, collect(n.name) AS entities""",
            uuid=uuid,
            group=group,
        )
        rows = await result.data()
        assert len(rows) == 1 and rows[0]['content'] == BODY
        assert {'MCPProbeLinh', 'MCPProbeLab'} <= set(rows[0]['entities'])
        result = await db.run(
            """MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity)
                WHERE r.group_id=$group AND $uuid IN r.episodes
                RETURN a.name AS source, b.name AS target, r.name AS relation, r.fact AS fact""",
            uuid=uuid,
            group=group,
        )
        facts = await result.data()
        assert any(
            {row['source'], row['target']} == {'MCPProbeLinh', 'MCPProbeLab'}
            and (
                any(word in row['fact'].lower() for word in ('work', 'employ', 'làm'))
                or row['relation'].upper() in ('WORKS_FOR', 'EMPLOYED_BY', 'EMPLOYS')
            )
            for row in facts
        ), 'Expected employment fact not read back from Neo4j'
