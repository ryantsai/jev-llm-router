# JEV LLM Router

Python tools for intent-aware model and reasoning selection with local Codex.
Two independent integrations are available:

| | Option 1: native coordinator | Option 3: HTTP gateway |
| --- | --- | --- |
| Entry point | `jev-codex` | `jev-router` |
| Mechanism | Main Codex session chooses a configured subagent | Proxy selects the upstream model for Responses API requests |
| Authentication | Existing native Codex sign-in; account/model access still applies | Separate upstream API credential and API billing |
| Additional running service | None | Local Python gateway |
| Main chat model | Unchanged; only the worker uses the selected pair | Client may still display its baseline model |
| Configuration | Generated skill and custom agent TOML files | Custom-provider configuration pointing to the gateway |
| Guide | [Native Codex setup](docs/native-codex.md) | [Gateway setup and API reference](GATEWAY.md) |

Neither option changes the model picker of ordinary hosted ChatGPT conversations.
Option 1 targets local Codex clients that expose custom agents and native subagent
tools. Automatic delegation is instruction-driven, not a hard per-message hook.

## Install

Python 3.11 or newer:

```powershell
git clone https://github.com/ryantsai/jev-llm-router.git
cd jev-llm-router
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

On macOS/Linux, use `python3 -m venv .venv` and `source .venv/bin/activate`.
Installing the package alone does not modify any Codex settings.

## Option 1: use native Codex agents

```text
User request + conversation context
                   |
          Existing main Codex model
          applies $jev-intent-router
                   |
       choose allowed model AND effort
                   |
         One native execution worker
                   |
       targeted validation and final answer
```

Preview, then install into the project you actually open in Codex:

```powershell
jev-codex catalog
jev-codex install --project "C:\Projects\MyProject" --auto --dry-run
jev-codex install --project "C:\Projects\MyProject" --auto
jev-codex doctor --project "C:\Projects\MyProject"
```

Use `--user` instead of `--project ...` for personal agents and guidance across
projects. Do not install the same skill in both scopes without reviewing name
collisions. User agents honor `CODEX_HOME`; user skills live under
`$HOME/.agents/skills`. Restart Codex or begin a new session after installation.

Without `--auto`, the skill is explicit-only. In the **Codex composer**, enter:

```text
$jev-intent-router Investigate the intermittent failure and add a regression test.
```

The default catalog contains **12 worker presets**: Luna, Terra, Sol, and Astra,
each at low, medium, and high effort. The initial task defaults are Luna/low,
Terra/medium, Sol/high, and Astra/medium. The coordinator can select a different
allowed effort independently. These are heuristics, not benchmark guarantees.
`xhigh` is opt-in through configuration and still requires model/client support.

**Already using Option 3?** Restore your native Codex provider configuration
before using Option 1. The installer deliberately does not rewrite
`config.toml`, log you in, or stop the gateway. See
[Switching from the gateway](docs/native-codex.md#switching-from-option-3).

The installer offers idempotent installs, conflict-checked updates, dry runs,
status, and uninstall. It preserves unrelated instructions/configuration and
refuses to overwrite locally edited managed files. See the
[full guide](docs/native-codex.md) for caps, safeguards, and removal.

## Option 3: use the API routing gateway

The original gateway remains available without behavior changes. It supports
HTTP/SSE streaming, tool/vision payload passthrough, optional semantic
classification, and persistent affinity for linked/opaque model state.

Follow [GATEWAY.md](GATEWAY.md) for credentials, `router.toml`, Codex provider
setup, supported endpoints, operational boundaries, and security guidance.
Do not mix the two integrations until their effective providers and billing
paths have been verified on your machine.

## Development and validation

```powershell
python -m pytest --cov=jev_llm_router --cov-report=term-missing
python -m compileall -q src tests
```

CI runs on Windows/Linux with Python 3.11/3.13. Native routing tests validate
artifact generation, caps, TOML, installer lifecycle, and safety behavior; gateway
tests use mock upstreams. Neither proves live model availability, actual desktop
delegation, routing quality, or token savings. Run the manual acceptance checks in
[the native guide](docs/native-codex.md#manual-acceptance-checks) before relying on it.

MIT licensed; see [LICENSE](LICENSE).
