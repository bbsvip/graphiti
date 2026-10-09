# MCP ingest reliability implementation plan

> **For agentic workers:** Use superpowers:executing-plans, task-by-task with regression tests before production changes.

**Goal:** Accepted graph writes remain recoverable and only validated output reaches Neo4j; clients distinguish acceptance from persistence.

**Architecture:** Collect raw Responses events, reconcile text per output/content index, validate locally and account once. Store episode jobs in SQLite alongside the persistent runtime volume, with bounded retry and explicit status. Prepare a source-only pi-graphiti fork, retaining the installed package for rollback.

**Tech Stack:** Python/venv, installed OpenAI SDK, Pydantic, SQLite, asyncio, MCP 2, TypeScript/Node.

**Spec:** User's confirmed MCP defects and constraints in this session.

## Global constraints
- Preserve custom OAuth/admin/Infinity/providers and models.
- Preserve manual changes in server/tests/test_oauth_security.py. Owner subsequently approved edits only within test_oauth_inference_preserves_schema_and_requires_completed_status; everything outside is byte-identical. Never change construction_marketplace source/session.
- No database resets, fake properties, fabricated JSON, credential changes, or bulk transcript replay.
- Regression RED before implementation; local venv only; no edits to installed node_modules.
- Remote success requires redeploy and Neo4j readback, not HTTP 200/queued.

## Review focus
- SDK eagerly parses schema output before the terminal usage event.
- Done/terminal text duplicates accumulated deltas or disagrees with them.
- Restart between graph commit and queue acknowledgement.
- Concurrent session-expiry recovery and ambiguous write transport failures.
- Serializable configured extraction types and persistent episode UUID semantics.

### 1. OAuth + usage
- [x] Characterize installed SDK accumulator/final response, reproduce terminal-empty/multiple-chunk stream with real SDK over MockTransport.
- [x] Add tests for completion, refusal, incomplete, failed, missing terminal, empty, bad JSON/schema and no double-count.
- [x] Collect raw events without eager parsing, reconcile representations without concatenating copies, validate Graphiti schema; log structure only.
- [x] Record exactly one usage row per request after validation (preserving terminal provider tokens).

### 2. Durable queue + MCP contract
- [x] Inspect Graphiti UUID creation/reuse and persisted graph commit boundary.
- [x] RED tests: two ordered episodes, retained failed payload, bounded retry, processing cancellation and restart, duplicate submit, conflicts.
- [x] Persist jobs/status/attempts/reference time/extraction schema; recover pending processing work; retain failures; stable episode UUID; sequential group workers and safe shutdown.
- [x] Expose acknowledgement UUID/status and a status tool, wire path to OPENAI_STATE_DIR; no claims that queued means stored.
- [x] GREEN queue/runtime/core tests.

### 3. Local client fork
- [x] Copy only package source/manifests/license/tests (not dependency/runtime folders) into integrations/pi-graphiti.
- [x] RED tests for isError, JSON-RPC error, nested ErrorResponse, backend acknowledgement and session expiry.
- [x] Surface application errors; bounded reinitialization only for definite session rejection/read operations; no blind ambiguous writes/replay.
- [x] Preserve UUID across local spool replay, truthful graph add responses; tests/type checks and activation/rollback documentation.

### 4. Verification/deployment handoff
- [x] Run related suites and available no-DB core gate; inspect final diff, preserve protected manual changes outside the explicitly approved mock update.
- [x] Update server/README.md and reliability runbook with verified/pending status.
- [x] Prepare transcript recovery candidates only (read-only, no replay), keep private content out of logs/version control.
- [x] Supply operator-only rebuild/rollback commands and isolated diagnostic episode/readback procedure (NOT executed).

## Release gates still pending
- [x] Owner-approved update of the three protected OAuth mock cases; full non-integration server suite: 68 passed, 1 skipped, 1 deselected.
- [ ] Independent code review (no reviewer subagent tool available in this session).
- [ ] Authorized service-only rebuild/redeploy and opt-in Neo4j episode/entity/fact readback.
- [ ] Activate fork only in a new Graphiti session after live verification; do not reload construction_marketplace.
- [ ] Individually approve/reconcile recovery candidates; no bulk replay.
