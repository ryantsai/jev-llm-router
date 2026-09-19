# JEV LLM Router

A Python **Responses API routing gateway** for local Codex clients. It selects a model and reasoning effort from task intent, then forwards the original request to an upstream API. It is not a subagent coordinator or a replacement desktop application.

```text
Codex desktop / CLI / IDE local runtime
             | HTTP + SSE, local gateway token
             v
      JEV LLM Router
      rules -> optional classifier -> continuity / budget checks
             | upstream API credential (never sent to the client)
             v
      Luna / Terra / Sol / Astra
```

**Scope:** the shipped integration targets Codex's custom-provider configuration. It does not intercept normal hosted ChatGPT conversations, change ChatGPT's model picker, or turn a ChatGPT subscription into API credits. This implementation uses an upstream API credential and its associated billing. It does not read browser cookies or Codex OAuth credential files. See the [official authentication documentation](https://developers.openai.com/codex/auth/).

## Implemented

- Intent rules for English and Traditional Chinese; optional structured-output classifier for ambiguous tasks.
- Independent model and effort selection, explicit overrides, configurable model IDs, and maximum tier/effort limits.
- HTTP Responses requests, incremental SSE forwarding, tool calls, multimodal payload passthrough, compaction, stored-response retrieval/deletion/cancellation, and input-item listing.
- Persistent, bounded SQLite routing affinity for response IDs, tool call IDs, opaque state, and optional session IDs. Reference values are hashed; prompts, outputs, and credentials are not stored.
- Authentication, loopback binding by default, browser-origin rejection, request-size and concurrency limits, credential separation, fixed upstream URLs, redirect rejection, and no automatic generation retries.
- Local routing previews, configuration diagnostics, upstream model-list checks, and route headers/logs.

The current test suite uses **mock upstreams**, not paid model calls. Desktop end-to-end behavior and account-specific model permissions must be checked on the target machine.

## Requirements and installation

Python 3.11 or newer. Use a virtual environment:

```powershell
git clone https://github.com/ryantsai/jev-llm-router.git
cd jev-llm-router
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item examples/router.toml router.toml
```

On macOS/Linux, use `python3 -m venv .venv`, `source .venv/bin/activate`, and `cp examples/router.toml router.toml` instead.

Provide two **different** environment variables to the gateway process:

| Variable | Purpose |
| --- | --- |
| `JEV_ROUTER_TOKEN` | A random local gateway token, at least 24 ASCII characters. Share only this token with Codex. |
| `JEV_UPSTREAM_API_KEY` | Your upstream API credential. Keep it on the gateway side. |

For OpenAI, obtain your credential through the secure API key setup flow; do not paste it into a chat, commit, or config file. `.env.example` lists the names only: the program does **not** auto-load `.env`. Supply secrets with your shell, service manager, or secret store. A local gateway token can be generated without creating an API credential:

```powershell
$env:JEV_ROUTER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Keep that same gateway token in the environment used to launch Codex. A newly generated token on every terminal launch will not match an already-running gateway.

Validate and run:

```powershell
jev-router --config router.toml doctor
jev-router --config router.toml doctor --check-upstream
jev-router --config router.toml serve
```

`doctor --check-upstream` calls only `/models` and reports configured model IDs that are not listed. It never sends a generation. Listing does not guarantee generation permission, supported effort, or all tool capabilities. Model IDs and effort capabilities in `examples/router.toml` are a configurable documentation snapshot, not automatic account discovery.

A secret-free, offline preview is available before starting the service:

```powershell
jev-router route "Investigate an intermittent race condition"
```

Expected initial policy: Sol / high.

## Configure Codex

Back up `~/.codex/config.toml` (`$HOME\.codex\config.toml` on Windows). Merge [examples/codex-config.toml](examples/codex-config.toml) into it. Do not overwrite unrelated MCP, permissions, or project settings. Top-level settings must appear before TOML tables; merge existing `[features]` rather than creating a duplicate table.

```toml
model = "gpt-5.6-terra"
model_provider = "jev_router"
model_reasoning_effort = "medium"

[features]
enable_request_compression = false

[model_providers.jev_router]
name = "JEV Intent Router"
base_url = "http://127.0.0.1:8765/v1"
env_key = "JEV_ROUTER_TOKEN"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 650000
```

The real baseline model lets Codex retain known model metadata. The gateway intercepts `gpt-5.6-terra` by default, along with `auto` and `jev-auto`. Other configured model IDs are explicit selections. The desktop may continue displaying the baseline model; the gateway's `X-Jev-Model` / `X-Jev-Effort` response headers and route log identify the actual selection. Response bodies and SSE events are not rewritten to disguise the upstream model.

Fully quit and relaunch the desktop client after changing its environment/configuration. A GUI launched from a desktop shortcut may not inherit variables exported in a terminal. Verify CLI routing first from a shell with the gateway token, then validate the desktop launch environment. The gateway cannot change the model context window or tool capabilities advertised by Codex; use models with compatible capabilities and conservative client limits.

This configuration deliberately uses **HTTP/SSE**, not WebSockets. Compressed request bodies return 415, so request compression is disabled. HTTP/SSE transport, provider authentication, compression, and retry settings are documented in the [Codex configuration reference](https://developers.openai.com/codex/config-reference/) and [advanced configuration guide](https://developers.openai.com/codex/config-advanced/).

To undo the integration, restore your backed-up provider/model settings and restart Codex. No desktop files, credentials, or settings are modified by installing the Python package.

## Routing policy

The initial policy is an editable heuristic, **not a guarantee that a task needs a particular model**:

| Tier | Default model ID | Initial effort | Initial task pattern |
| --- | --- | --- | --- |
| Luna | `gpt-5.6-luna` | low | Translation, extraction, formatting, small mechanical edits |
| Terra | `gpt-5.6-terra` | medium | Bounded everyday coding; default when intent is unclear |
| Sol | `gpt-5.6-sol` | high | Concurrency, intermittent bugs, architecture, security reviews |
| Astra | `gpt-6-astra` | medium | Cross-system work, entire-repository changes, formal verification |

Only user-role text informs the rules/classifier. Tool output, system/developer instructions, and binary image content are not treated as routing instructions. A short "continue" can reuse earlier user intent when the request includes it. Other non-text content is forwarded unchanged, but the classifier does not inspect it.

`max_tier` and `max_effort` cap automatic selections. Explicit requests exceeding them fail rather than silently escalating. These are **selection caps**, not dollar budgets or a token quota. The gateway does not automatically rerun failed tasks, claim benchmark-based optimality, or downgrade unavailable models.

For semantic classification, set:

```toml
[routing]
mode = "hybrid"
classifier_tier = "luna"
classifier_timeout = 5
classifier_max_chars = 6000
```

Merge those values into the existing routing section. Confident rule matches do not call a classifier. Ambiguous/default cases send a bounded recent user-text excerpt directly to the upstream Responses endpoint, with no tools and `store=false`. The result must match a fixed JSON schema and the configured model/effort allowlist. Timeout, refusal, invalid JSON, or upstream errors fall back to rules. Classification itself consumes upstream usage and can add latency. It is not called recursively through the gateway.

`respect_client_effort=false` lets the router replace Codex's baseline effort in automatic mode. Explicit model requests preserve a supplied `reasoning.effort`; a trusted `X-Jev-Effort` header overrides it. For example, native API clients can set `X-Jev-Model: sol` and `X-Jev-Effort: low`. The automatic baseline ID cannot also mean "force Terra"; use `X-Jev-Model: terra` or remove that ID from `auto_models` for manual operation.

## Continuity and safe model changes

A gateway cannot assume every upstream model understands every other model's opaque reasoning state. JEV therefore routes **independent tasks**, not arbitrary tool continuations:

1. An independent request is classified and assigned a model/effort.
2. A response chain using `previous_response_id`, current tool call references, or encrypted reasoning/compaction state stays on its recorded route. Opaque content is never decrypted, removed, or rewritten.
3. New user turns can be reclassified when the input is self-contained and has no pinned opaque/linked state. Historical completed tool calls before that user turn do not alone lock the new turn.
4. Optional `X-Jev-Session` pins an entire session. Do not configure one shared static session header for unrelated chats.

Unknown/expired opaque state returns **409**, rather than guessing a model. Preserve the SQLite state directory across restarts. To recover, start a fresh task or explicitly supply the **original model and effort**; selecting arbitrary replacements is not safe. Configuration/model-mapping changes can also require a fresh task. Affinity has configurable TTL/capacity; eviction is fail-closed, not automatic rerouting.

Compaction responses are returned intact, including retained messages and encrypted items, consistent with [OpenAI's compaction guidance](https://developers.openai.com/api/docs/guides/compaction). Long Codex histories with opaque state may consequently remain on their initial model. Deliberate cross-model history migration is outside this version's scope.

## API and diagnostics

All endpoints except `/healthz` require `Authorization: Bearer <gateway-token>`.

| Endpoint | Behavior |
| --- | --- |
| `POST /v1/responses` | Route and proxy JSON or SSE generation |
| `POST /v1/responses/compact` | Select/preserve model; do not inject generation-only reasoning fields |
| `GET /v1/responses/{id}` | Retrieve stored upstream response |
| `DELETE /v1/responses/{id}` | Delete stored upstream response |
| `POST /v1/responses/{id}/cancel` | Forward cancellation for an upstream background response |
| `GET /v1/responses/{id}/input_items` | Forward input-item listing and query parameters |
| `GET /v1/models` | Local configured catalog; explicitly not an account-access guarantee |
| `POST /v1/router/preview` | Rule/affinity preview, no classifier call, no state mutation |
| `GET /healthz` | Process liveness only, not upstream readiness |

Example preview from an already-configured PowerShell environment:

```powershell
$headers = @{ Authorization = "Bearer $env:JEV_ROUTER_TOKEN" }
$body = @{ model = "auto"; input = "Find this race condition" } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8765/v1/router/preview `
  -Method Post -Headers $headers -ContentType application/json -Body $body
```

Upstream status codes, request IDs, rate-limit headers, and retry hints are preserved. Network errors become sanitized 502/504 responses. A mid-stream failure produces an SSE error, never a replay. Streaming connections are closed on cancellation/disconnection, including downstream send failures. Disconnecting does not guarantee an upstream provider stops charging already-started computation; stored/background responses have their separate cancellation endpoint.

## Security and operational limits

Use **one gateway process/worker and one trusted upstream account**. SQLite affinity is not a distributed session store, and the token is not tenant isolation. Anyone holding the gateway token can spend on that account and use the supported stored-response endpoints. Do not expose it directly to the Internet. For remote use, add TLS, proper authentication, network controls, rate limiting, and operational monitoring.

TLS verification is enabled; redirects are not followed. Upstream URL overrides come only from operator configuration, never request headers. Plain HTTP upstreams are rejected except explicitly allowed loopback development endpoints. `trust_env=false` avoids silently using inherited proxies; enable it deliberately for a corporate proxy/certificate setup.

Logs contain generated request IDs, configured model/effort, and fixed routing reasons, not prompts or credentials. Request bodies still transit the gateway and the configured provider; do not confuse local logging policy with upstream data retention. Protect the machine, environment, and state directory. The SSE metadata observer is bounded; oversized metadata events can leave references untracked and trigger a later 409 rather than unsafe inference.

Not implemented: WebSocket proxying, Chat Completions translation, Conversations API state, files/audio APIs, standalone search endpoints, OAuth/subscription passthrough, automatic cross-model opaque-state migration, cost accounting, or quality-driven retry/escalation. Normal embedded Responses tools are forwarded, subject to upstream model support. Desktop/cloud features using other endpoints are not covered by this gateway.

## Development

```powershell
python -m pytest --cov=jev_llm_router --cov-report=term-missing
python -m compileall -q src tests
```

CI runs the suite on Windows and Linux with Python 3.11 and 3.13. Tests cover routing, classifier failures, explicit overrides, budget caps, persistent/expired/conflicting affinity, tool loops, compaction, split UTF-8 SSE, compressed upstream responses, disconnects, concurrency admission, credentials, validation, lifecycle endpoints, and CLI previews.

The model/effort snapshot was checked against the [Codex model guide](https://developers.openai.com/codex/models/), [Luna model page](https://developers.openai.com/api/docs/models/gpt-5.6-luna), and [Astra model page](https://developers.openai.com/api/docs/models/gpt-6-astra) on 2026-09-19. Recheck availability and evaluate routing quality on representative real tasks before relying on it for unattended work.

MIT licensed; see [LICENSE](LICENSE).
