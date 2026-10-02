# graph-service

Graph service is a fast api server implementing the [graphiti](https://github.com/getzep/graphiti) package.

## Container Releases

The FastAPI server container is automatically built and published to Docker Hub when a `v*.*.*` tag is pushed (the same tags that publish `graphiti-core` to PyPI).

**Image:** `zepai/graphiti`

**Available tags:**
- `latest` - Latest stable release
- `0.30.2` - Specific version (matches graphiti-core version)

**Platforms:** linux/amd64, linux/arm64

The automated release workflow:
1. Triggers on `v*.*.*` tag pushes (and can be run manually via `workflow_dispatch`)
2. Waits for that `graphiti-core` version to be available on PyPI
3. Builds multi-platform Docker image
4. Tags with version number and `latest`
5. Pushes to Docker Hub

Only stable releases are built automatically (pre-release versions are skipped).

## Running Instructions

The REST server uses OpenAI's **Sign in with ChatGPT** public-client OAuth flow for
LLM requests and a self-hosted Infinity server for embeddings and reranking. No
OpenAI API key is needed. This changes the REST server's provider wiring; the core
library and the separate MCP server retain their existing configuration. The LLM
can also use a keyless local OpenAI-compatible endpoint while Infinity stays the
embedding and reranking provider.

For the existing Neo4j at `192.168.1.11`, copy `.env.remote.example` to `.env`
at the repository root, enter its actual `NEO4J_PASSWORD` and adjust `NEO4J_USER`
if needed. Keep an existing `.env` and add these settings instead of overwriting it.
From the repository root:

```sh
docker compose -f docker-compose.remote.yml up --build -d
```

This connects over Bolt at `bolt://192.168.1.11:7687` and does not start another
Neo4j container. Neo4j's HTTP browser at port `7474` is separate from the Bolt
connection used by Graphiti. The existing server startup creates Graphiti indices
and constraints in the configured database.

To create a new local Neo4j container instead, use the original Compose file:

```sh
docker compose up --build -d graph neo4j
```

Open [the administration page](http://127.0.0.1:8000/admin). On first launch,
create an administrator password, then use **Continue with ChatGPT**. Authorize
ChatGPT plan usage in OpenAI's browser flow. After sign-in, load the model catalog,
select the primary and small LLM models, and save connections. The account's model
catalog supplies the choices; completing inference confirms model access.

The default Infinity URL is `http://192.168.1.11:7997` and the embedding model is
`BAAI/bge-m3`. Choose the reranker model that your Infinity `/models` response lists
with the `rerank` capability. No local model is downloaded by Graphiti. The page
validates model capabilities before saving. Infinity must be reachable from the
container, and must provide `/models`, `/embeddings`, and `/rerank`.

### Local LLM with a custom base URL

At `/admin`, choose **OpenAI-compatible URL · llama.cpp / Ollama** for the LLM
connection, enter the server's base URL **including `/v1`**, then load its model
catalog. Choose the primary and small LLM models (the same model may fill both
roles) and save connections. This mode does not require a ChatGPT sign-in or send
OAuth credentials to the custom server. The existing Infinity URL, embedding
model and reranker remain independent of the LLM URL.

Examples: `http://192.168.1.11:8080/v1` for a llama.cpp server listening on port
8080, or `http://192.168.1.11:11434/v1` for Ollama on its default port. These are
examples, not automatically discovered running services. In Docker, `localhost`
refers to the Graphiti container; use the reachable LAN hostname/IP of the LLM
server.

The adapter uses `/models` and non-streaming `/chat/completions` relative to the
base URL, requests `json_schema` structured output, and validates returned JSON
against Graphiti's extraction schema. The server/model must support this contract.
Truncated, refused or schema-invalid responses fail instead of entering the graph.
No automatic model downloads or fallback to a different provider occur. This mode
supports local servers without API-key authentication. HTTP usage fields feed the
same dashboard under **LLM · OpenAI-compatible URL**; missing usage is not estimated.
Switch back to **ChatGPT OAuth** and choose eligible account models to use plan
quota again; saved OAuth registrations are retained.

Provider API references: [Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility)
and [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).

`OPENAI_STATE_DIR` defaults to `.openai-runtime` for local runs. Compose mounts it
at `/app/.openai-runtime` using a named volume, so accounts, rotated credentials,
the administrator password hash, connection settings and token history survive
container recreation. Protect this volume as credentials. Do not share it between
independent deployments or delete it unless you intend to remove their sign-ins
and history. Refreshes are serialized with a file lock, including across workers
sharing this volume.

The page shows application input/output/cached tokens, daily usage in UTC, and
usage by provider/model. Cache tokens are a subset of input tokens. Failed LLM
requests without an upstream usage payload are counted without estimating billed
tokens. This is **application usage**, not an estimate of the account's remaining
Codex quota. Use the **Manage usage** link for account-wide limits and app access.

Optional environment settings (also editable connections on the web):

| Setting | Default / purpose |
| --- | --- |
| `LOCAL_MODEL_URL` | `http://192.168.1.11:7997` |
| `EMBEDDING_MODEL_NAME` | `BAAI/bge-m3` |
| `RERANKER_MODEL_NAME` | Select a running Infinity reranker on the page |
| `MODEL_NAME` | Select eligible OpenAI models on the page |
| `LLM_PROVIDER` | `oauth` or `custom`; saved web settings override this |
| `LLM_BASE_URL` | Base URL including `/v1`, used only in custom LLM mode |
| `GRAPHITI_PORT` | `8000`, including the OAuth callback port in Compose |
| `GRAPHITI_BIND_HOST` | `127.0.0.1`; initialize the administrator before exposing the page |
| `OPENAI_CALLBACK_PORT` | `8000` for runs outside Compose; must match the browser-facing port |

Saved web settings override model/Infinity environment defaults. `EMBEDDING_DIM`
still controls the existing graph embedding dimension (default `1024`). The
adapter rejects mismatched dimensions rather than silently truncating vectors.
Changing embedding models changes the vector space even with the same dimension:
existing graph embeddings must be regenerated separately. This page never
rewrites graph data or migrates existing embeddings.

For FalkorDB, use `docker compose --profile falkordb up --build -d graph-falkordb falkordb`
and open `http://127.0.0.1:8001/admin`. It has its own OAuth volume and callback port.
The existing FalkorDB concurrency limitation still applies.

### Docker on a remote host

The documented public-client flow requires an exact loopback callback such as
`http://127.0.0.1:8000/auth/callback`. A browser on your laptop cannot reach the
remote container through its own loopback address. Keep Compose's private binding
and forward the port first:

```sh
ssh -L 8000:127.0.0.1:8000 user@docker-host
```

Then open `http://127.0.0.1:8000/admin` on that laptop and complete sign-in. Use the
corresponding configured port for both the tunnel and callback if you change it.
HTTPS/domain callbacks are not substituted for the loopback callback in this flow.

For local development, configure `server/.env` using `server/.env.example`.
Use a project-local `venv/`; from the repository root on Windows PowerShell:

```powershell
python -m venv venv
uv pip sync --python venv/Scripts/python.exe server/requirements.txt
uv pip install --python venv/Scripts/python.exe --no-deps -e .
$env:GRAPHITI_TELEMETRY_ENABLED = 'false'
Set-Location server
../venv/Scripts/python.exe -m uvicorn graph_service.main:app --port 8000 --no-access-log
```

On Linux, use `venv/bin/python` instead of `venv/Scripts/python.exe` and set
environment variables with the shell's native syntax. The editable local core
keeps the REST server aligned with this checkout, as the Compose build does.
`server/requirements.txt` contains the frozen server and test dependencies exported
from `server/uv.lock`. After changing server dependencies, regenerate it from the
repository root with `uv export --project server --frozen --extra dev --no-emit-project --no-hashes --output-file server/requirements.txt`.

Disable query-string access logging so OAuth authorization codes and ID-token
hints do not enter access logs. The Docker command already disables it. For a
reverse proxy, apply the same rule to `/auth/callback`. `/healthcheck` remains a
server health check, not an OAuth/Infinity/inference readiness check. Graph
database access is still required during startup.

Run the isolated connection/security tests without an OpenAI account or database:

```powershell
$env:GRAPHITI_TELEMETRY_ENABLED = 'false'
$env:PYTHONPATH = "$PWD/server"
venv/Scripts/python.exe -m pytest server/tests/test_oauth_connections.py server/tests/test_oauth_security.py server/tests/test_custom_llm.py -o asyncio_mode=auto
```

Verified: isolated OAuth/security and custom-LLM request tests; administration UI
save/reload and provider switching with a mock LLM catalog; live Infinity embedding
and reranking requests. The custom LLM protocol tests validate schema adherence,
model selection, usage accounting and independence from OAuth credentials.
Pending: inference against an actual configured llama.cpp/Ollama server, a Docker
image build, and live OAuth ingest/search. Provide a running LLM URL/model through
the page to enable the local LLM path; no local LLM service is started by Graphiti.

The live FalkorDB test now requires `GRAPHITI_RUN_OAUTH_INTEGRATION=1` and an
absolute `OPENAI_STATE_DIR` pointing to an already signed-in runtime. Isolated
tests do not verify a live OAuth ingest/search pipeline. Live verification
requires a signed-in account, saved model choices, a ready Infinity service, and
a reachable graph database, and consumes the account's quota.

Official request contracts: [registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in),
[models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference),
[preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations),
and [accounts, refresh and usage](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions).

Swagger remains at `/docs` and Redoc at `/redoc`. The remote Neo4j browser is at
`http://192.168.1.11:7474`; the local Compose Neo4j browser is at `http://localhost:7474`.
