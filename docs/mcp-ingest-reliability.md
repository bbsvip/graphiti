# MCP ingest reliability — local fix and deployment gate

## Status

The implementation is on `fix/mcp-ingest-reliability`. Publishing this branch does
not deploy the service or verify remote ingestion.
The installed pi-graphiti 0.6.0 and its configuration are unchanged. No Neo4j data,
OAuth volumes, credentials, construction_marketplace source, or running sessions
were changed. Remote endpoint: `http://192.168.1.11:8123/mcp/`.

**Remote repair/readback is NOT verified. Do not replay transcripts or snapshots.**
The owner approved updating only the three parametrized OAuth mock cases. Those
updates now pass; all bytes outside the target test function were preserved.
Independent review and authorized deployment/readback remain pending.

## Causes demonstrated locally

- OpenAI SDK 1.92.2's stream state accumulates deltas into a snapshot, but
  `response.completed` and `get_final_response()` parse the **terminal response**.
  They do not rebuild an empty terminal `output` from prior delta/done items.
  A real-SDK synthetic SSE fixture reproduces valid multi-chunk JSON being lost
  by the old adapter when terminal `output=[]`. Merely calling final_response is
  insufficient. The remote stream's exact shape still needs the new structural
  diagnostics after deployment; no authenticated remote capture was performed.
- SDK structured stream parsing can raise on done/schema before the adapter sees
  terminal usage. Raw Responses events avoid eager parsing; the request still
  asks for the same strict Graphiti JSON schema/model/provider.
- Old usage status was recorded as upstream `completed` before JSON/schema
  validation. A validation failure therefore did not increase failures.
- Old queue held closures only, caught errors then task_done, and discarded failed
  episodes on shutdown/recreation. Also, core previously required a supplied UUID
  to already exist: a stable new job UUID could not be used without fixing that
  creation branch. New episodes are NOT pre-saved as placeholder nodes.
- The installed client ignored isError and nested `{error:...}`, invented a queued
  success instead of returning the acknowledgement, and reused expired sessions.
  MCP union result wrappers could also hide successful search results. A malformed
  JSON-RPC array result could masquerade as acceptance; a new RED/GREEN regression
  now rejects it at the parser without replay.

## New contract and limits

- OAuth output is reconciled by output/content index: deltas, text done, content/
  item done, then terminal items. Duplicate representations are not concatenated.
  Refusal, failed/incomplete, missing terminal, empty/conflicting output, bad JSON
  and bad schema are errors. Pydantic validates before output is returned.
- One usage row and one token-tracker update per request, after validation; actual
  terminal provider tokens remain counted even on extraction failure. Missing
  terminal usage is not estimated. Dashboard failures represent adapter outcome,
  **not** proof of Neo4j ingestion. Ingest outcome is the job journal.
- Diagnostics contain event counts, statuses, part counts and character lengths,
  never prompts, output, refusal bodies, OAuth tokens/codes or credentials.
- Integrated queue: `$OPENAI_STATE_DIR/episodes.sqlite3`, inside the existing
  protected OAuth/runtime volume. Standalone queue: `GRAPHITI_QUEUE_PATH` or
  `.graphiti-queue/episodes.sqlite3`; mount that directory persistently in Docker.
  One process owns a journal (file lock); do not run multiple workers against it.
- `add_memory` returns uuid/group_id/status/attempts/error and a truthful message.
  queued means accepted into the journal, **not stored in Neo4j**. Poll
  `get_episode_status(uuid, group_id)` for queued/processing/succeeded/failed.
- Same UUID/same payload returns existing state; conflicting payload is rejected.
  Default reference time is fixed at acceptance, not regenerated on retry.
  Workers are sequential per group; cancellation retains queued work. Restart
  recovers processing and queued jobs in journal order, retaining attempts.
- At most three lifetime job attempts by default. Only classified transient
  transport/429/5xx errors retry automatically. Output/schema/refusal errors fail
  once and remain recoverable, including their original payload.
- After repairing a cause, `retry_episode(uuid, group_id)` is an **explicit operator
  action**, not automatic client replay. It retains payload/identity and does not
  reset the attempt budget. Exhausted jobs require operator review.
- Recovery checks the scoped graph UUID before re-extraction, covering the graph
  commit-before-journal-ack crash window. Core extraction writes episodic node and
  extracted entities/facts in one transaction. Optional saga/community work is
  post-commit: an existing node alone cannot prove those effects finished; such
  recovery fails as `PostCommitRecoveryRequired` for manual reconciliation instead
  of spending more extraction tokens or claiming unverified success.
- Persisted extraction schemas must match configured Pydantic types on recovery.
  A changed/missing type fails visibly; no substitute empty model is invented.
- Keep failed/succeeded journal rows for reconciliation. Payloads are private;
  backup and protect this volume. No retention/pruning policy is introduced here.
  On Windows, Unix mode bits are not NTFS ACLs: the operator must protect both the
  runtime directory and `.pi/recovery/` with suitable owner-only ACLs.

## Local verification

Latest checks (2026-10-08):

| Gate | Observed result |
|---|---|
| OAuth/queue/core UUID/MCP regressions | 30 passed; live readback test skipped (not opted in) |
| Authoritative no-DB core gate | 472 passed, 11 skipped |
| Focused standalone MCP unit gate | 79 passed, 1 skipped |
| Full server, non-integration | 68 passed, 1 skipped, 1 deselected |
| Broad standalone MCP suite | 82 passed, 32 failed, 2 errors, 1 skipped |
| Local client fork | 20 passed; TypeScript check passed |
| Changed production Python modules | Targeted Ruff and Pyright passed |

The installed MCP SDK is also characterized: after DELETE terminates a session,
a write using that old session gets HTTP 404 `Session not found` **before tool
dispatch**, without another Graphiti call. This supports only the client's bounded
recovery of that definite rejection, not replay of ambiguous transport failures.

Use the existing project `venv/`; never globally install packages. Optional core/
MCP test-provider dependencies are recorded in `server/requirements-test-extras.txt`.
Windows commands (Linux: replace `venv/Scripts/python.exe` with `venv/bin/python`):

```powershell
$env:GRAPHITI_TELEMETRY_ENABLED = 'false'
venv/Scripts/python.exe -m pytest -p no:cacheprovider --basetemp=.test-tmp/regressions -c server/pyproject.toml server/tests/test_oauth_stream_regression.py server/tests/test_durable_queue.py server/tests/test_episode_idempotency.py server/tests/test_mcp_job_contract.py server/tests/test_mcp_runtime.py -q
venv/Scripts/python.exe -m pytest -p no:cacheprovider --basetemp=.test-tmp/server -c server/pyproject.toml server/tests -m 'not integration' -q
venv/Scripts/python.exe -m pyright --pythonpath venv/Scripts/python.exe server/graph_service/oauth_client.py server/graph_service/mcp_runtime.py mcp_server/src/services/episode_store.py mcp_server/src/services/queue_service.py
cd integrations/pi-graphiti
npm ci --ignore-scripts
npm test
npm run check
```

The regression gate includes multi-chunk/terminal disagreement, refusal/incomplete/
schema failure, actual installed SDK final-response characterization, two ordered
jobs, bounded retries, duplicate/conflict submissions, safe shutdown, recovery
from an **os._exit process crash**, and the commit-before-ack window. Client tests
use a real loopback HTTP server, including concurrent session expiry/handshake,
application errors, acknowledgement forwarding and held ambiguous snapshot writes.

With explicit owner approval, only
`test_oauth_inference_preserves_schema_and_requires_completed_status[completed|incomplete|failed]`
was updated: the mock now has async `responses.create`, uses a validated SDK Response
with actual output message content, and asserts the strict `text.format` JSON schema.
Before the change all three failed; afterwards all three and the full non-integration
server suite passed. No production code was changed to accommodate a test fake.
A private pre-edit snapshot is retained under `.pi/recovery/`; AST-bounded byte
comparison verified that everything outside this one test function is unchanged.

The broad standalone MCP suite includes unmarked live/stress tests; without their
OpenAI-key/stdio/services setup they fail, and two async-performance tests lack
`performance_benchmark`. See [named verification failures](verification-failures.md).
These failures were reported, not relabelled/skipped in production tests. No-DB
unit verification can ignore those three live-test files explicitly; that is a
focused unit gate, not a green full MCP suite. The authoritative core gate is the
command/ignore list in `.github/workflows/unit_tests.yml`, with all four database
DISABLE flags set. Test logs are under ignored `.test-tmp/`.

## Deployment (operator approval/access required)

No remote administrative session was used and no redeploy has been performed.
On the actual deployment host, from its Graphiti checkout, preserve the existing
`.env`, OAuth volume and database. Pause new client writes during the transition;
old in-memory pending work cannot be recovered by the new journal retrospectively.
Do not manipulate another project's running sessions.

1. Review the local diff and recorded gates, including the owner-approved mock update.
2. Back up the runtime volume privately using the existing operator procedure.
   Record the old image before rebuilding:
   ```sh
   docker image tag "$(docker compose -f docker-compose.remote.yml images -q graph)" graphiti-mcp:pre-ingest-fix
   ```
3. Transfer only reviewed Graphiti changes. The remote compose already uses
   `INSTALL_LOCAL_CORE=true`; keep it so the stable UUID creation fix is included.
4. Rebuild only this service (no database/volume reset):
   ```sh
   docker compose -f docker-compose.remote.yml up --build -d --no-deps graph
   docker compose -f docker-compose.remote.yml logs --tail=100 graph
   ```
5. Confirm the catalog includes get_episode_status/retry_episode and configured
   models remain OAuth gpt-5.5 (both sizes), BAAI/bge-m3 and the existing Infinity
   reranker at `192.168.1.11:7997`. Do not change providers or credentials.
6. Run the opt-in readback gate below. Only then activate the local client fork.

### Required live gate: one small diagnostic episode

Run from an authorized machine that can reach MCP and Bolt. Supply the existing
Neo4j password through your normal secret environment, never command-line literals.
Generate a UUID **once and keep it** for this diagnostic; re-running polls the same
job rather than generating another write. Use only the diagnostic group.

```powershell
$env:GRAPHITI_TELEMETRY_ENABLED = 'false'
$env:GRAPHITI_VERIFY_MCP_INGEST = '1'
$env:GRAPHITI_VERIFY_EPISODE_UUID = [guid]::NewGuid().ToString() # once; retain for reruns
$env:GRAPHITI_VERIFY_GROUP = 'mcp_diag_ingest'
$env:GRAPHITI_MCP_URL = 'http://192.168.1.11:8123/mcp/'
$env:NEO4J_URI = 'bolt://192.168.1.11:7687'
# NEO4J_USER / NEO4J_PASSWORD: use existing protected credentials
venv/Scripts/python.exe -m pytest -p no:cacheprovider -c server/pyproject.toml server/tests/test_live_neo4j_mcp_int.py -v
```

This test submits only `MCPProbeLinh works for MCPProbeLab.` if the UUID has no
journal row, polls to succeeded, then directly queries Neo4j for the exact episode
content, both mentioned entities and an employment fact correlated to the episode
UUID. HTTP 200/queued alone cannot pass. A failed/ambiguous result must be reconciled
by UUID before any retry; do not rerun with a new UUID to mask the failure. No
cleanup/delete/reset is performed. This gate has **not been executed remotely**.

## Client activation and rollback

Follow [the local fork guide](../integrations/pi-graphiti/RELIABILITY.md). Keep the
installed npm package intact; do not uninstall it (upstream uninstall hooks may
perform teardown). Enable only one extension instance. No settings were edited
or sessions reloaded by this task.

For server rollback, use the saved old image with an operator-created compose
image override and `up -d --no-build --no-deps graph`. Never run `down -v` or
remove `.openai-runtime`. Retain the new journal: old code will not consume it;
pause ingestion and reconcile it before returning to the repaired version.
The old image still has the known ingest bug; rollback is not a successful repair.

## Lost-note recovery candidates — no replay

The specified construction_marketplace transcript was read only. Three explicit
`graph add` calls were found at transcript lines **1660, 3084, 4193**. Their arguments
and original responses are retained privately in
`.pi/recovery/construction_marketplace-graph-add-candidates.json` (git-ignored).
This is a candidate list, not a claim that all three are missing in Neo4j. No
source/session was modified, no snapshot was added, and no transcript was replayed.
After a verified live gate, reconcile each candidate's original group/content and
reference timestamp, obtain approval for the selected adds, assign stable UUIDs
once, and check readback
one at a time. Never bulk-replay the transcript or old snapshot spool automatically.

## Review and decisions still requiring input

Author self-review only (no subagent reviewer tool available); get an independent
review before merging/deploying. The limited protected mock update is approved and
verified. Remaining decisions: deployment operator/access; activation in a **new Graphiti
session only**; and approval of individual recovery candidates. Existing old spool
entries lack proof of prior acceptance and need manual review before activation.
