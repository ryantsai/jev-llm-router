"""Generate and safely manage native Codex skills and custom-agent presets.

This is an offline installer, not a hook, proxy, or model execution engine.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .codex_assets import AUTO_BLOCK, BEGIN, END, HANDOFF, PURPOSES, SKILL, WORKER
from .config import Config, EFFORTS, TIERS

SKILL_NAME = "jev-intent-router"
# Intersection of this project's effort vocabulary and documented Codex config
# values. UI modes such as Max/Ultra are not assumed to be valid config values.
NATIVE_EFFORTS = ("low", "medium", "high", "xhigh")
SCHEMA_VERSION = 1
SKILL_FILES = {"SKILL.md", "agents/openai.yaml", "references/routing-policy.md",
               "references/handoff.md"}
AGENT_KEY = re.compile(r"agents/jev_(luna|terra|sol|astra)_(low|medium|high|xhigh)\.toml\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class Target:
    scope: str
    skill_dir: Path
    agent_dir: Path
    instructions: Path

    @classmethod
    def project(cls, root: Path) -> Target:
        root = root.expanduser().absolute()
        return cls("project", root / ".agents" / "skills" / SKILL_NAME,
                   root / ".codex" / "agents", root / "AGENTS.md")

    @classmethod
    def user(cls, home: Path | None = None, codex_home: Path | None = None) -> Target:
        home = (home or Path.home()).expanduser().absolute()
        codex = (codex_home or Path(os.environ.get("CODEX_HOME", home / ".codex")))
        codex = codex.expanduser().absolute()
        return cls("user", home / ".agents" / "skills" / SKILL_NAME,
                   codex / "agents", codex / "AGENTS.md")

    @property
    def manifest(self) -> Path:
        return self.skill_dir / "install.json"

    @property
    def layout(self) -> str:
        # Project installs stay portable when the repository is moved.
        return "." if self.scope == "project" else str(self.agent_dir.parent)

    def path(self, key: str) -> Path:
        if key.startswith("skill/") and key[6:] in SKILL_FILES:
            return self.skill_dir / key[6:]
        if AGENT_KEY.fullmatch(key):
            return self.agent_dir / key[7:]
        raise ValueError(f"unrecognized managed path: {key!r}")


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def catalog(config: Config) -> list[dict[str, str]]:
    """Return only configured, capped pairs accepted by native Codex config."""
    result = []
    for tier in TIERS[:TIERS.index(config.routing.max_tier) + 1]:
        spec = config.models[tier]
        efforts = [e for e in NATIVE_EFFORTS if e in spec.supported_efforts
                   and EFFORTS.index(e) <= EFFORTS.index(config.routing.max_effort)]
        if not efforts:
            raise ValueError(f"{tier}: no native effort within the configured cap; use low/medium/high/xhigh")
        if spec.effort not in NATIVE_EFFORTS:
            raise ValueError(f"{tier}: native default effort must be low/medium/high/xhigh")
        # A default above the cap is reduced to the greatest allowed effort.
        default = max((e for e in efforts if EFFORTS.index(e) <= EFFORTS.index(spec.effort)),
                      key=EFFORTS.index, default=efforts[0])
        for effort in efforts:
            result.append({"tier": tier, "model": spec.id, "effort": effort,
                           "agent": f"jev_{tier}_{effort}", "default_effort": default})
    return result


def render(config: Config, *, automatic: bool = False) -> dict[str, bytes]:
    """Build portable files. Never serialize upstream URLs or credentials."""
    rows = catalog(config)
    fallback = TIERS[min(TIERS.index(config.routing.default_tier), TIERS.index(config.routing.max_tier))]
    policy = ["# Installed routing catalog\n",
              "These are requested configurations, not verified account capabilities.\n",
              f"Tier ceiling: **{config.routing.max_tier}**. Effort ceiling: **{config.routing.max_effort}**.\n",
              f"Uncertain-task fallback: **{fallback}**, using its effective default effort below.\n",
              "No pair outside this table may be selected by this skill. Caps constrain this\n"
              "catalog only; they are not a platform security or billing boundary.\n",
              "| Tier | Model ID | Effort | Exact custom agent | Effective tier default |",
              "| --- | --- | --- | --- | --- |"]
    files = {"skill/SKILL.md": SKILL.encode("utf-8"),
             "skill/references/handoff.md": HANDOFF.encode("utf-8")}
    for row in rows:
        tier, effort = row["tier"], row["effort"]
        policy.append(f'| {tier} | `{row["model"]}` | {effort} | `{row["agent"]}` | {row["default_effort"]} |')
        # JSON basic strings are also valid TOML basic strings for these values.
        values = {"name": row["agent"], "description": f"{PURPOSES[tier]}; {effort} reasoning. JEV leaf worker.",
                  "model": row["model"], "model_provider": "openai",
                  "model_reasoning_effort": effort, "developer_instructions": WORKER}
        text = "# Generated by jev-codex. Update through the installer.\n"
        text += "\n".join(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in values.items())
        text += "\n\n[agents]\nenabled = false\n"
        files[f'agents/{row["agent"]}.toml'] = text.encode("utf-8")
    policy += ["", "Only low/medium/high/xhigh are emitted by this installer. Gateway-only",
               "none/max values are not translated into native config or UI Ultra mode.",
               "Verify each needed pair in your signed-in client before relying on it.",
               "Workers select the built-in openai provider; no authentication or sandbox",
               "policy is overridden. Existing base-URL overrides can still affect it.",
               "Do not combine with Option 3 without reviewing the effective provider.",
               "", "A route example: intermittent concurrency failure -> Sol/high, provided",
               "that pair is in this table. This is a heuristic, not a benchmark guarantee.", ""]
    files["skill/references/routing-policy.md"] = "\n".join(policy).encode("utf-8")
    files["skill/agents/openai.yaml"] = ('''interface:
  display_name: "JEV Intent Router"
  short_description: "Delegate tasks by model and reasoning effort"
  default_prompt: "Use $jev-intent-router to choose a worker for this task."
policy:
  allow_implicit_invocation: ''' + str(automatic).lower() + "\n").encode("utf-8")
    return files


def _check_path(path: Path) -> None:
    """Refuse symlinks, Windows reparse points, and non-regular target files."""
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"refusing symlink/reparse point: {part}")
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"parent is not a directory: {part}")
    if path.exists() and not path.is_file():
        raise ValueError(f"target is not a regular file: {path}")


def _read(path: Path) -> bytes | None:
    _check_path(path)
    if path.exists():
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError(f"managed file exceeds 2 MiB safety limit: {path}")
        return path.read_bytes()
    return None


def _manifest(target: Target) -> dict | None:
    data = _read(target.manifest)
    if data is None:
        return None
    try:
        value = json.loads(data)
        if (not isinstance(value, dict) or value.get("schema") != SCHEMA_VERSION
                or type(value.get("schema")) is not int
                or value.get("scope") != target.scope or value.get("layout") != target.layout
                or not isinstance(value.get("files"), dict)
                or type(value.get("automatic")) is not bool
                or type(value.get("instructions_created")) is not bool):
            raise ValueError("invalid or mismatched install manifest; check scope and CODEX_HOME")
        for key, digest in value["files"].items():
            target.path(key)
            if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                raise ValueError("invalid manifest digest")
        if set(k for k in value["files"] if k.startswith("skill/")) != {f"skill/{p}" for p in SKILL_FILES}:
            raise ValueError("incomplete install manifest")
        return value
    except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("cannot parse install manifest") from exc


def _check_owned(target: Target, previous: dict) -> None:
    for key, digest in previous["files"].items():
        content = _read(target.path(key))
        if content is not None and _digest(content) != digest:
            raise ValueError(f"locally modified managed file; preserve/reconcile it first: {target.path(key)}")


def _strip_block(content: bytes, automatic: bool) -> bytes:
    block = AUTO_BLOCK.encode("utf-8")
    if automatic:
        if content.count(block) != 1:
            raise ValueError("managed AGENTS.md block changed or missing; reconcile it first")
        content = content.replace(block, b"", 1)
    if BEGIN.encode() in content or END.encode() in content:
        raise ValueError("unowned or duplicate JEV routing block in AGENTS.md")
    return content


def _write(path: Path, content: bytes | None, mode: int | None = None) -> None:
    _check_path(path)
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".jev-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _apply(changes: dict[Path, bytes | None], *, dry_run: bool) -> list[dict[str, str]]:
    snapshots = {path: _read(path) for path in changes}
    changes = {p: data for p, data in changes.items() if data != snapshots[p]}
    actions = [{"action": "delete" if data is None else "create" if snapshots[p] is None else "update",
                "path": str(p)} for p, data in changes.items()]
    if dry_run:
        return actions
    modes = {p: stat.S_IMODE(p.stat().st_mode) if snapshots[p] is not None else None for p in changes}
    applied = []
    try:
        for path, data in changes.items():
            if _read(path) != snapshots[path]:
                raise ValueError(f"file changed during install: {path}")
            _write(path, data, modes[path])
            applied.append(path)
    except (OSError, ValueError):
        # Best-effort rollback for ordinary I/O failures, not a crash-safe transaction.
        # Never overwrite an edit that appeared after our write.
        for path in reversed(applied):
            if _read(path) == changes[path]:
                _write(path, snapshots[path], modes[path])
        raise
    return actions


def install(target: Target, config: Config, *, automatic: bool = False,
            update: bool = False, dry_run: bool = False) -> list[dict[str, str]]:
    files = render(config, automatic=automatic)
    previous = _manifest(target)
    if previous:
        _check_owned(target, previous)
    changes: dict[Path, bytes | None] = {}
    for key, content in files.items():
        path = target.path(key)
        current = _read(path)
        if current is not None and (previous is None or key not in previous["files"]):
            raise ValueError(f"refusing to overwrite an unowned file: {path}")
        changes[path] = content
    if previous:
        for key in previous["files"].keys() - files.keys():
            changes[target.path(key)] = None
    current_instructions = _read(target.instructions)
    base = _strip_block(current_instructions or b"", bool(previous and previous["automatic"]))
    created = previous["instructions_created"] if previous else current_instructions is None
    if automatic:
        # Prevent knowingly adding guidance to a file Codex will ignore.
        override = _read(target.instructions.with_name("AGENTS.override.md"))
        if override and override.strip():
            raise ValueError("AGENTS.override.md shadows AGENTS.md; use explicit skill invocation or reconcile it")
        changes[target.instructions] = base + AUTO_BLOCK.encode("utf-8")
    elif previous and previous["automatic"]:
        changes[target.instructions] = None if created and not base else base
    manifest = {"schema": SCHEMA_VERSION, "scope": target.scope, "layout": target.layout,
                "files": {k: _digest(v) for k, v in files.items()},
                "automatic": automatic, "instructions_created": created}
    changes[target.manifest] = _json(manifest)  # Commit ownership last.
    if previous and not update and any(_read(p) != data for p, data in changes.items()):
        raise ValueError("installation differs; rerun with --update after reviewing the dry run")
    return _apply(changes, dry_run=dry_run)


def uninstall(target: Target, *, dry_run: bool = False) -> list[dict[str, str]]:
    previous = _manifest(target)
    if previous is None:
        return []
    _check_owned(target, previous)
    changes: dict[Path, bytes | None] = {target.path(k): None for k in previous["files"]}
    if previous["automatic"]:
        content = _read(target.instructions)
        base = _strip_block(content or b"", True)
        changes[target.instructions] = None if previous["instructions_created"] and not base else base
    changes[target.manifest] = None
    return _apply(changes, dry_run=dry_run)


def status(target: Target) -> dict:
    previous = _manifest(target)
    if previous is None:
        return {"installed": False, "intact": False, "scope": target.scope, "files": []}
    files = []
    for key, expected in previous["files"].items():
        data = _read(target.path(key))
        state = "missing" if data is None else "ok" if _digest(data) == expected else "modified"
        files.append({"path": str(target.path(key)), "state": state})
    block_ok = True
    if previous["automatic"]:
        try:
            _strip_block(_read(target.instructions) or b"", True)
        except ValueError:
            block_ok = False
    return {"installed": True, "intact": block_ok and all(f["state"] == "ok" for f in files),
            "scope": target.scope, "automatic": previous["automatic"],
            "instructions_ok": block_ok, "files": files}
