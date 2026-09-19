"""Offline CLI for Option 1; does not start Codex or inspect login credentials."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path

from pydantic import ValidationError

from .codex_native import Target, catalog, install, status, uninstall
from .config import load_config


def diagnostics(target: Target) -> list[str]:
    """Advisory checks only; Codex remains the authority on effective config."""
    warnings = ["Model access and live delegation are not verified by this offline command."]
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    paths = {target.agent_dir.parent / "config.toml"}
    if target.scope == "project":
        paths.add(codex_home / "config.toml")
    for path in sorted(paths):
        if not path.is_file():
            continue
        try:
            with path.open("rb") as stream:
                cfg = tomllib.load(stream)
            for section in ("model_providers", "agents", "features"):
                if section in cfg and not isinstance(cfg[section], dict):
                    raise ValueError("invalid table")
            if not isinstance(cfg.get("model_providers", {}).get("openai", {}), dict):
                raise ValueError("invalid provider table")
        except (OSError, ValueError):
            warnings.append(f"Cannot parse Codex configuration: {path}")
            continue
        if cfg.get("model_provider", "openai") != "openai":
            warnings.append(f"Non-openai coordinator provider in {path}; restore native provider settings for Option 1.")
        if cfg.get("openai_base_url") or cfg.get("model_providers", {}).get("openai", {}).get("base_url"):
            warnings.append(f"Built-in openai provider has a URL override in {path}; review it to avoid double routing.")
        if cfg.get("agents", {}).get("enabled") is False:
            warnings.append(f"Subagents are disabled in {path}.")
        if cfg.get("features", {}).get("multi_agent") is False:
            warnings.append(f"Legacy multi_agent flag is disabled in {path}; check your client version.")
    if os.environ.get("OPENAI_BASE_URL"):
        warnings.append("OPENAI_BASE_URL is set; review the effective native provider endpoint.")
    if target.instructions.with_name("AGENTS.override.md").is_file():
        warnings.append("AGENTS.override.md may shadow the automatic routing guidance.")
    if target.scope == "project":
        warnings.append("Codex must trust this project to load project configuration; review nested instruction overrides.")
    warnings.append("Profiles, managed policy, parent directories, duplicate agent names, and CLI overrides can change effective settings.")
    return warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install native Codex intent routing (Option 1); no gateway required.")
    parser.add_argument("--config", type=Path, help="JEV router TOML for model IDs, efforts, and caps; no credentials needed")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog", help="Show generated worker pairs without writing files or making requests")
    for name in ("install", "status", "doctor", "uninstall"):
        command = commands.add_parser(name)
        scope = command.add_mutually_exclusive_group(required=True)
        scope.add_argument("--project", type=Path, help="Target project root (explicit path required)")
        scope.add_argument("--user", action="store_true", help="Install for this user across projects")
        command.add_argument("--codex-home", type=Path, help="Override CODEX_HOME for --user only")
        if name in {"install", "uninstall"}:
            command.add_argument("--dry-run", action="store_true", help="Report planned changes without writing")
        if name == "install":
            command.add_argument("--auto", action=argparse.BooleanOptionalAction, default=None,
                                 help="Enable implicit invocation and append managed AGENTS.md guidance; default off on first install")
            command.add_argument("--update", action="store_true", help="Update only unmodified files owned by a previous install")
    args = parser.parse_args(argv)
    try:
        if args.command == "catalog":
            result = {"workers": catalog(load_config(args.config)), "account_access_verified": False}
        else:
            if args.codex_home and not args.user:
                parser.error("--codex-home requires --user")
            target = Target.user(codex_home=args.codex_home) if args.user else Target.project(args.project)
            if args.command == "install":
                automatic = args.auto if args.auto is not None else status(target).get("automatic", False)
                actions = install(target, load_config(args.config), automatic=automatic,
                                  update=args.update, dry_run=args.dry_run)
                result = {"dry_run": args.dry_run, "automatic": automatic, "actions": actions,
                          "note": "Start a new Codex session. Main model and login are unchanged. Invoke $jev-intent-router."}
            elif args.command == "uninstall":
                result = {"dry_run": args.dry_run, "actions": uninstall(target, dry_run=args.dry_run)}
            else:
                result = status(target)
                if args.command == "doctor":
                    result["warnings"] = diagnostics(target)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if args.command not in {"status", "doctor"} or result["intact"] else 1
    except ValidationError:
        # Validation errors may embed input values, including accidentally supplied secrets.
        print("Invalid router configuration; check model IDs, effort lists, caps, and TOML sections.", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"jev-codex: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
