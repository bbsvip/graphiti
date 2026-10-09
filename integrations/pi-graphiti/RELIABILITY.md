# Local pi-graphiti fork (0.6.0-mcpfix.1)

Source-only fork of the installed pi-graphiti 0.6.0 (upstream MIT license and
attribution retained). Existing installed package/config/session files were not
modified. This private package is not published. README.md/CHANGELOG.md preserve
upstream documentation; some upstream standalone scripts are not bundled here.

## Verified changes

- Reject JSON-RPC errors, isError, and ErrorResponse in text, structured content
  or MCP union wrappers. Missing/wrong-ID results are not successful tool calls.
- Preserve actual add acknowledgement/status/UUID instead of inventing success.
  `stored:false` for queued/accepted/spooled/unknown; queue acceptance is not
  proof of an episode/entity/fact in Neo4j.
- Reinitialize once for definite HTTP 404 `Session not found`, which the MCP
  transport returns before tool dispatch. Keep the same write UUID. Do not
  replay write requests for arbitrary 404, timeout, network/500/protocol failure.
- Concurrent expired calls share replacement initialization and cannot reset a
  newer session. Concurrent callers wait for notifications/initialized too.
- Generate a write UUID once, retain it across snapshot spooling and replay.
  Ambiguous outcomes return unknown or are retained with `manualReview:true`;
  they are NEVER automatically replayed. Application rejections are not reported
  as queued success or endlessly retried in the local spool.
- Old 0.6.0 spool entries without admission UUIDs are assigned a stable migration
  identity but held for manual review, never auto-replayed. Review original
  group/content/name in Neo4j before allowing them to replay.
- Decode successful union-wrapped entity/fact/episode results too.

Tests use an actual loopback HTTP server, not the live deployment. Dependencies
are local to this directory; no global installation or edits to installed package:

```powershell
Set-Location G:\self_project\graphiti\integrations\pi-graphiti
npm ci --ignore-scripts
npm test
npm run check
```

`npm test` covers MCP errors, expiry recovery/exhaustion/concurrency, ambiguous
writes, acknowledgement forwarding, held snapshot identity, legacy spool holds
and wrapped reads. `npm run check` runs TypeScript with no emission.

## Activation — only AFTER verified server redeploy

The repaired client sends new explicit episode UUIDs. The OLD server cannot
create these correctly. **Do not activate this fork against the old deployment.**
First follow [the server deployment and Neo4j readback gate](../../docs/mcp-ingest-reliability.md).
Do not enable both npm and local versions, or their hooks can double-submit.
Do not reload or edit a construction_marketplace session.

For an isolated one-process smoke test, open a **new PowerShell** in the Graphiti
repo (not construction_marketplace). Process-only environment overrides leave
saved config and other running processes unchanged:

```powershell
Set-Location G:\self_project\graphiti
$env:PI_GRAPHITI_URL = 'http://192.168.1.11:8123/mcp/'
$env:PI_GRAPHITI_GROUP_ID = 'mcp_diag_client'
$env:PI_GRAPHITI_PROJECT_SCOPING = 'false'
$env:PI_GRAPHITI_NUDGE_INTERVAL = '100000000'
$env:PI_GRAPHITI_FLUSH_ON_COMPACT = 'false'
$env:PI_GRAPHITI_FLUSH_ON_SHUTDOWN = 'false'
$env:PI_GRAPHITI_REVIEW_ENABLED = 'false'
$env:PI_GRAPHITI_CORRECTION_DETECTION = 'false'
$env:PI_GRAPHITI_SPOOL = 'false'
pi --no-extensions -e 'G:\self_project\graphiti\integrations\pi-graphiti\src\index.ts'
```

Explicit `-e` still loads with `--no-extensions`; the old npm extension and its
hooks do not load in this new process. Start with `/graph status` and reads.
Any further diagnostic write consumes provider quota and needs approval. Exit
this smoke process to rollback; no persistent settings are changed.

For persistent activation **after approval**, privately back up
`C:\Users\Admin\.pi\agent\settings.json`, then inspect `pi list` to find the exact
existing package source. Preserve that package on disk and disable its extensions
using a filtered package declaration, adding the local path, for example:

```json
{
  "packages": [
    { "source": "npm:pi-graphiti", "extensions": [] },
    "G:/self_project/graphiti/integrations/pi-graphiti"
  ]
}
```

This is only the relevant fragment: preserve all OTHER package/settings entries.
Use the exact old source shown by pi list (it may include @0.6.0), not a guessed
replacement. Do not run `pi remove`/uninstall: upstream uninstall hooks can
perform backend teardown. Keep `pi-graphiti-config.json` unchanged. Start a new
Graphiti session, or reload only a specifically approved Graphiti session; do
not globally reload other running project sessions. Pi local package paths load
from that source directory without copying it over node_modules.

## Reconcile a held write

Retain the UUID printed by graph add or stored in the private spool. Query the
server `get_episode_status(uuid,group_id)`. For succeeded, confirm Neo4j readback
and archive the held local copy. For failed, repair the cause and explicitly use
`retry_episode` (budget is not reset). For an unknown/no-row outcome, verify the
original group/name/content in Neo4j first, then seek approval to submit with the
same UUID/payload. Legacy entries without a historical UUID need content-level
reconciliation. Never release the entire spool or replay old snapshots as a batch.

Spool content is private. Keep a backup before manual editing. Release only a
reviewed entry by setting its `manualReview` flag to false; keep its UUID and
payload unchanged. Retention limits may move old/oversize/failed entries into
`dead-letter.jsonl`; review that file too. This fork adds no bulk-release command.

## Rollback

- Isolated smoke: exit the new process; original npm package/settings were intact.
- Persistent: restore the saved settings fragment (enable the original source,
  remove the local source); open/reload only the approved Graphiti session.
- Keep the fork and held spool files for diagnosis. Do not delete credentials,
  Neo4j data or volumes. The original 0.6.0 has the confirmed bugs; rollback is
  not evidence of a repaired pipeline. Do not run old auto-replay hooks against
  ambiguous entries. Coordinate rollback with the server image and pause writes.

## Pending

No live Pi session was activated/reloaded, no fork was published and no remote
write/readback was performed. Server protected mock updates, independent review,
remote deployment, operator live verification and individual recovery approvals
are still required. No guarantee of exactly-once ingestion across arbitrary
external producers: UUID conflict checking and scoped readback cover this queue's
own atomic extraction commit boundary, not other producers' partial writes.
