# Option 1: native Codex coordinator and workers

This integration uses a **skill plus native custom agents**. The existing main
Codex session performs semantic intent classification using the task and its
conversation context. It delegates substantive work to a configured model/effort
pair and validates the result. Python generates and installs the artifacts; it
does not intercept model requests, run a classifier API, or launch Codex.

The main chat's selected model and effort do not change. The routing announcement
reports the selected worker configuration, not backend execution telemetry.
Inspect the native subagent thread/runtime metadata when verifying actual use.

## Prerequisites

Use a current local Codex desktop, CLI, or IDE runtime that supports standalone
custom-agent TOML files and native subagent tools. The machine must have a native
Codex sign-in with access to the selected models. Project-level configuration may
require the project to be trusted. Do not bypass managed policy or permissions.

No JEV gateway token or upstream API credential is required for this option.
Existing Codex authentication determines usage and billing: native ChatGPT sign-in
uses the access available to that account; API-key sign-in uses API billing.
Delegation adds coordinator and worker work, so it does not guarantee lower usage.
The installer neither reads login files nor changes your authentication.

## Install into a project

After installing the Python package, use an explicit project root:

```powershell
jev-codex install --project "C:\Projects\MyProject" --dry-run
jev-codex install --project "C:\Projects\MyProject"
```

For automatic coordinator instructions on substantive tasks:

```powershell
jev-codex install --project "C:\Projects\MyProject" --auto --update --dry-run
jev-codex install --project "C:\Projects\MyProject" --auto --update
```

On an initial installation `--update` is unnecessary. Without `--auto`,
`agents/openai.yaml` disables implicit invocation: request `$jev-intent-router`
in the **Codex composer**, not PowerShell. With `--auto`, the installer enables
implicit invocation and appends a delimited block to the project's `AGENTS.md`.
This encourages automatic delegation; it is not an enforced pre-message hook.
Other instructions, unavailable tools, or client behavior can affect invocation.

```text
$jev-intent-router Fix the intermittent race condition and add a regression test.
$jev-intent-router Use Terra at low effort for this bounded formatting change.
$jev-intent-router 幫我分析跨系統的登入問題，保留現有的權限與安全限制。
```

The skill explicitly honors user opt-outs and requested pairs within the installed
catalog. Missing/forbidden pairs must be reported rather than silently replaced.
A trivial answer may remain on the main model without a worker, and must not be
misrepresented as a cheaper model execution.

Generated project layout (default configuration):

```text
.agents/skills/jev-intent-router/
  SKILL.md
  agents/openai.yaml
  references/routing-policy.md
  references/handoff.md
  install.json
.codex/agents/
  jev_luna_low.toml       # low/medium/high per tier: 12 files total
  jev_terra_medium.toml
  jev_sol_high.toml
  jev_astra_medium.toml
  ...
AGENTS.md                 # modified only with --auto
```

All operational skill content is self-contained after installation: Codex does not
need `jev-codex` on PATH while routing. Keep `install.json` with the generated files
for future updates/removal. Project installations are portable when moved with
the repository. Review generated guidance before committing it to a shared repo.

## User-wide installation

```powershell
jev-codex install --user --auto --dry-run
jev-codex install --user --auto
jev-codex doctor --user
```

User skills go to `$HOME/.agents/skills/jev-intent-router`; agents and optional
`AGENTS.md` go under `CODEX_HOME` (default `$HOME/.codex`). Use
`--codex-home "C:\CodexProfiles\Native"` with `--user` to target an alternate
Codex home explicitly. The client must use that same home. This does not isolate
user skills: their location remains `$HOME/.agents/skills`.

Choose project OR user scope for this skill. Simultaneous scopes, other plugins,
or agents with the same names can cause duplicate/shadowed definitions; the
installer does not rewrite other installations. A user manifest records its Codex
home and refuses removal through a different home to avoid deleting wrong files.

## Configure models, effort, and caps

The installer reuses the existing JEV `Config` schema. It reads `models.*`,
`routing.default_tier`, `routing.max_tier`, and `routing.max_effort` from an explicit
`--config` file (or `JEV_ROUTER_CONFIG`). It exports no upstream URLs, tokens,
organization settings, or gateway credentials. It does not call `credentials()`.
Gateway `routing.mode`, classifier settings, and `respect_client_effort` do not
control the native coordinator: semantic classification happens in the main model.
The existing `jev-router route` command remains a gateway-rule preview, not a
simulation or guarantee of the native coordinator's decision.

Minimal optional file, using the shared default model mappings:

```toml
[routing]
default_tier = "terra"
max_tier = "astra"
max_effort = "high"
```

```powershell
jev-codex --config native-router.toml catalog
jev-codex --config native-router.toml install --project "C:\Projects\MyProject" --auto --update
```

Default pairs are all four tiers at low/medium/high; defaults per tier are
Luna/low, Terra/medium, Sol/high, and Astra/medium. Changing the cap removes disallowed
worker files on update, so the skill's catalog and installed presets stay aligned.
The fallback tier is capped too, and tier defaults above the effort cap are lowered
to an allowed effort. Empty native effort intersections fail before any writes.

To customize model IDs or supported efforts, supply all four `[models.<tier>]`
tables as required by the shared schema; use `examples/router.toml` as a starting
point. Validate them against the account's actual capabilities. `supported_efforts`
is operator configuration, **not automatic discovery**. The native installer emits
only low/medium/high/xhigh, the shared vocabulary's intersection with documented
Codex configuration values. It ignores none/max in capability lists and rejects
those as tier defaults. It never converts them into native UI Max/Ultra settings.
To permit xhigh, raise `max_effort` and include it in the relevant supported list.

Caps are a generated catalog/instruction policy, not a platform enforcement or
billing boundary. They do not restrict your main model, other skills, or agents
spawned outside this coordinator. No hard token/dollar accounting is implemented.

## Execution and safety rules

The installed skill requires a bounded, self-contained brief with the task goal,
relevant evidence, allowed write scope, acceptance criteria, previous attempts,
and actual validation results. It uses conversation context to interpret short
continuations, and treats retrieved content as data rather than routing authority.

Only one worker is active at a time. The instruction budget is two worker starts
per user task: initial execution and at most one evidence-based escalation. The
coordinator must not perform the same implementation concurrently. Completed
workers are closed before a replacement starts. Workers set `[agents] enabled =
false` and are told not to invoke the routing skill or start nested Codex processes.
This relies on the supported runtime for native-tool disabling and on instructions
for behaviors outside those tools; it is not an independent sandbox.

A new model/effort uses a fresh native worker and a plain-language handoff, not
copied encrypted reasoning, response IDs, or compaction payloads. Resume only the
same task and pair. Review and research remain read-only unless edits are requested.
The generated agents do not override sandbox mode or approval policy.

Only concrete reasoning failures or materially expanded scope justify escalation.
Unavailable models, permission denials, rate limits, and missing test infrastructure
do not. Report blockers instead of automatic retries or hidden fallback models.
The coordinator verifies changes and distinguishes observed tests from unrun checks.

## Switching from Option 3

Do not assume merging this PR or installing the skill disconnects an existing
proxy. The installer never modifies `config.toml` or the desktop model picker.
Back up the effective configuration and restore the native provider deliberately.
[examples/codex-native.toml](../examples/codex-native.toml) is a **merge-only example**:

```toml
model = "gpt-5.6-luna" # Optional lightweight coordinator, subject to account access
model_provider = "openai"
model_reasoning_effort = "low"

[agents]
enabled = true
max_concurrent_threads_per_session = 1
```

Top-level keys must precede TOML tables. Merge `[agents]` into an existing table;
do not create duplicate tables. You may keep your preferred main model instead.
Review active profiles, project configuration, `openai_base_url`, environment
base-URL overrides, and custom `model_providers.openai` settings. Stop the gateway
only when no other clients use it. Restart Codex under the intended sign-in and
configuration. Workers explicitly select `model_provider = "openai"`, but inherited
base-URL overrides can still affect that provider; this is not an endpoint firewall.

```powershell
jev-codex doctor --project "C:\Projects\MyProject"
```

`doctor` verifies generated-file integrity and gives advisory warnings about visible
provider overrides, disabled agents, and instruction shadowing. It makes no model
calls and does not read credentials. It is not a full Codex configuration resolver,
a connectivity test, or proof that the desktop loaded the files. `status`/`doctor`
return 0 for an intact install, 1 for missing/damaged installation, and 2 for errors;
advisory warnings alone do not change that integrity-based exit code.

## Update, disable automatic routing, and uninstall

```powershell
jev-codex install --project "C:\Projects\MyProject" --update --dry-run
jev-codex install --project "C:\Projects\MyProject" --update
jev-codex install --project "C:\Projects\MyProject" --no-auto --update
jev-codex uninstall --project "C:\Projects\MyProject" --dry-run
jev-codex uninstall --project "C:\Projects\MyProject"
```

Updates preserve the previous auto/manual mode unless `--auto`/`--no-auto` is
explicit. Reapply your `--config` file for custom mappings/caps; the manifest is
ownership metadata, not a replacement for source configuration. With no custom
configuration selected, updates generate the shared defaults.

The installer checks hashes before changing owned files and refuses unowned
collisions or edited managed files. There is no force-overwrite flag. Preserve your
edits separately and reconcile them first. Changes outside the managed `AGENTS.md`
block are retained byte-for-byte, including CRLF and Unicode. A non-empty adjacent
`AGENTS.override.md` blocks automatic installation instead of silently inserting
ineffective instructions. Nested or higher-level overrides still need manual review.

Symlinks/reparse points and path traversal in managed-file metadata are refused.
Dry runs write nothing. Ordinary writes use temporary files and per-file atomic
replacement, with best-effort rollback on I/O failures. This is not a crash-safe
multi-file transaction or protection against a hostile concurrent local process.
Do not run concurrent installers/editors; keep a backup for valuable configuration.
The installer leaves empty directories and unrelated files alone. Uninstall removes
only owned, unchanged files and its exact instruction block; native login,
`config.toml`, and the Option 3 gateway are untouched.

## Manual acceptance checks

These are **operator-run checks, not claimed automated desktop test results**.
They consume ordinary model usage. First inspect the generated files and run
`status`, then start a new native Codex session in the intended project.

1. Verify the skill and exact `jev_*` agents are available. Use a bounded explicit
   invocation and inspect the spawned thread's configured/actual model and effort.
2. Try formatting, routine implementation, an intermittent bug, and complex
   cross-system analysis. Check the disclosed route against scope and context;
   these heuristics are not an inflexible expected-model benchmark.
3. Send "continue" after a complex task. Verify the handoff retains the goal and
   relevant evidence rather than classifying the single word in isolation.
4. Request an unavailable pair or turn off native subagent tools. Verify that the
   failure is reported, not hidden behind a generic worker or direct API call.
5. Run a read-only review. Confirm no worker writes and no recursive worker starts.
   Try an out-of-catalog override; verify no silent cap violation.
6. Check that the main picker is unchanged and tests/results are not fabricated.
   Uninstall and confirm original instructions and provider settings remain intact.

## Documentation references

Checked against official documentation on 2026-09-19:

- [Codex subagents and standalone agent schema](https://developers.openai.com/codex/subagents)
- [Skills, discovery paths, and invocation metadata](https://developers.openai.com/codex/skills)
- [AGENTS.md discovery and precedence](https://developers.openai.com/codex/guides/agents-md)
- [Native configuration keys and effort values](https://developers.openai.com/codex/config-reference)
- [Model selection and account-dependent availability](https://developers.openai.com/codex/models)
- [Codex authentication](https://developers.openai.com/codex/auth)

The format and availability can evolve. Recheck the runtime's current documentation
and model access before unattended use.
