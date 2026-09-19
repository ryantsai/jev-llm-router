"""Offline generation, lifecycle, safety, packaging, and CLI tests for Option 1."""
import json
import tomllib
from pathlib import Path

import pytest

from jev_llm_router import codex_native as native
from jev_llm_router.codex_assets import AUTO_BLOCK, SKILL, WORKER
from jev_llm_router.codex_cli import main
from jev_llm_router.config import Config


@pytest.fixture
def target(tmp_path):
    return native.Target.project(tmp_path / "project")


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_default_catalog_and_toml():
    config = Config()
    rows = native.catalog(config)
    assert len(rows) == 12
    assert len({r["agent"] for r in rows}) == 12
    assert {r["effort"] for r in rows} == {"low", "medium", "high"}
    files = native.render(config)
    assert len(files) == 16
    for row in rows:
        doc = tomllib.loads(files[f'agents/{row["agent"]}.toml'].decode())
        assert doc["name"] == row["agent"]
        assert doc["model"] == row["model"]
        assert doc["model_reasoning_effort"] == row["effort"]
        assert doc["model_provider"] == "openai"
        assert doc["agents"]["enabled"] is False
        assert doc["developer_instructions"] == WORKER
        assert not {"sandbox_mode", "approval_policy", "env_key", "base_url"} & doc.keys()
    assert b"allow_implicit_invocation: false" in files["skill/agents/openai.yaml"]


def test_caps_model_mapping_and_effective_defaults():
    config = Config.model_validate({"routing": {"max_tier": "terra", "max_effort": "low"}})
    config.models["luna"].id = "my-luna"
    rows = native.catalog(config)
    assert [r["agent"] for r in rows] == ["jev_luna_low", "jev_terra_low"]
    assert rows[0]["model"] == "my-luna"
    assert all(r["default_effort"] == "low" for r in rows)
    text = native.render(config)["skill/references/routing-policy.md"].decode()
    assert "**terra**" in text and "jev_sol_" not in text


def test_supported_efforts_are_deduplicated_and_xhigh_is_opt_in():
    config = Config.model_validate({"routing": {"max_effort": "xhigh"}})
    config.models["luna"].supported_efforts = ["low", "low"]
    rows = native.catalog(config)
    assert len([r for r in rows if r["tier"] == "luna"]) == 1
    assert any(r["agent"] == "jev_astra_xhigh" for r in rows)
    assert not any(r["effort"] in {"none", "max"} for r in rows)


@pytest.mark.parametrize("effort", ["none", "max"])
def test_reject_gateway_only_default_efforts(effort):
    config = Config()
    config.models["luna"].effort = effort
    with pytest.raises(ValueError, match="native default"):
        native.render(config)


def test_reject_no_native_effort_under_cap():
    config = Config()
    config.routing.max_effort = "none"
    with pytest.raises(ValueError, match="no native effort"):
        native.render(config)


def test_config_details_and_environment_secrets_not_exported(monkeypatch):
    config = Config()
    config.upstream.base_url = "https://private.example/v1"
    config.upstream.organization = "private-org"
    monkeypatch.setenv("JEV_UPSTREAM_API_KEY", "never-export-this")
    monkeypatch.setenv("JEV_ROUTER_TOKEN", "nor-this-token")
    data = b"\n".join(native.render(config).values())
    for secret in (b"private.example", b"private-org", b"never-export-this", b"nor-this-token"):
        assert secret not in data


def test_instruction_guardrails_and_handoff():
    for text in ["does not change", "explicit user", "same configured pair", "two worker starts",
                 "ONE", "Do not invent", "unavailable", "繼續", "opaque", "rate limits"]:
        assert text in SKILL.replace("\n", " ")
    assert "not the coordinator" in WORKER
    assert "Do not invoke jev-intent-router" in WORKER
    handoff = native.render(Config())["skill/references/handoff.md"]
    assert b"read-only" in handoff and b"checks" in handoff


def test_dry_run_has_no_side_effects(target):
    actions = native.install(target, Config(), automatic=True, dry_run=True)
    assert len(actions) == 18
    assert not target.instructions.parent.exists()


def test_manual_install_is_idempotent_and_uninstall_round_trip(target):
    root = target.instructions.parent
    root.mkdir(parents=True)
    target.instructions.write_bytes(b"# Existing\r\nDo not change.\r\n")
    config_path = root / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_bytes(b'model_provider = "jev_router"\n')
    original = tree(root)
    assert native.install(target, Config())
    assert target.instructions.read_bytes() == original["AGENTS.md"]
    assert native.status(target)["intact"]
    assert not native.install(target, Config())
    assert native.uninstall(target, dry_run=True)
    assert native.status(target)["intact"]
    native.uninstall(target)
    assert tree(root) == original
    assert not native.uninstall(target)


def test_automatic_install_preserves_existing_bytes_and_external_edits(target):
    target.instructions.parent.mkdir(parents=True)
    initial = b"\xef\xbb\xbf# User guidance\r\nTraditional Chinese: \xe4\xb8\xad\xe6\x96\x87"
    target.instructions.write_bytes(initial)
    native.install(target, Config(), automatic=True)
    assert target.instructions.read_bytes() == initial + AUTO_BLOCK.encode()
    assert native.status(target)["automatic"]
    assert b"allow_implicit_invocation: true" in (target.skill_dir / "agents/openai.yaml").read_bytes()
    target.instructions.write_bytes(b"# New prefix\n" + target.instructions.read_bytes() + b"\nNew footer\n")
    native.uninstall(target)
    assert target.instructions.read_bytes() == b"# New prefix\n" + initial + b"\nNew footer\n"


def test_remove_auto_created_instructions_but_keep_unrelated_files(target):
    native.install(target, Config(), automatic=True)
    extra = target.skill_dir / "notes.txt"
    extra.write_text("keep")
    native.uninstall(target)
    assert not target.instructions.exists()
    assert extra.read_text() == "keep"


def test_update_removes_obsolete_presets_and_requires_flag(target):
    native.install(target, Config())
    config = Config.model_validate({"routing": {"max_tier": "luna", "max_effort": "low"}})
    before = tree(target.instructions.parent)
    with pytest.raises(ValueError, match="--update"):
        native.install(target, config)
    assert tree(target.instructions.parent) == before
    native.install(target, config, update=True)
    assert [p.name for p in target.agent_dir.glob("*.toml")] == ["jev_luna_low.toml"]
    assert native.status(target)["intact"]


def test_toggle_auto_off_restores_original_instructions(target):
    target.instructions.parent.mkdir(parents=True)
    target.instructions.write_bytes(b"original")
    native.install(target, Config(), automatic=True)
    native.install(target, Config(), automatic=False, update=True)
    assert target.instructions.read_bytes() == b"original"
    assert not native.status(target)["automatic"]


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_modified_files_abort_all_writes(target, operation):
    native.install(target, Config(), automatic=True)
    changed = target.agent_dir / "jev_sol_high.toml"
    changed.write_text("user-edited")
    before = tree(target.instructions.parent)
    with pytest.raises(ValueError, match="locally modified"):
        if operation == "install":
            native.install(target, Config(), update=True)
        else:
            native.uninstall(target)
    assert tree(target.instructions.parent) == before
    assert not native.status(target)["intact"]


def test_unowned_file_conflict_before_any_write(target):
    target.agent_dir.mkdir(parents=True)
    (target.agent_dir / "jev_terra_medium.toml").write_text("user-defined")
    before = tree(target.instructions.parent)
    with pytest.raises(ValueError, match="unowned"):
        native.install(target, Config())
    assert tree(target.instructions.parent) == before


def test_modified_auto_block_aborts_uninstall(target):
    native.install(target, Config(), automatic=True)
    target.instructions.write_text(target.instructions.read_text().replace("main Codex", "edited Codex"))
    before = tree(target.instructions.parent)
    with pytest.raises(ValueError, match="block changed"):
        native.uninstall(target)
    assert tree(target.instructions.parent) == before
    assert not native.status(target)["instructions_ok"]


def test_unowned_or_shadowed_auto_block_rejected(target):
    target.instructions.parent.mkdir(parents=True)
    override = target.instructions.with_name("AGENTS.override.md")
    override.write_text("important override")
    with pytest.raises(ValueError, match="shadows"):
        native.install(target, Config(), automatic=True)
    assert not target.skill_dir.exists()
    override.unlink()
    target.instructions.write_text(AUTO_BLOCK)
    with pytest.raises(ValueError, match="unowned or duplicate"):
        native.install(target, Config(), automatic=True)


def test_restore_missing_owned_file(target):
    native.install(target, Config())
    (target.agent_dir / "jev_luna_low.toml").unlink()
    assert not native.status(target)["intact"]
    native.install(target, Config(), update=True)
    assert native.status(target)["intact"]


@pytest.mark.parametrize("key", ["agents/../../secrets", "skill/../../outside", "agents/C:\\outside",
                                  "/tmp/outside", "skill/install.json", "agents/explorer.toml"])
def test_manifest_path_traversal_and_unowned_targets_rejected(target, key):
    native.install(target, Config())
    manifest = json.loads(target.manifest.read_text())
    manifest["files"][key] = "0" * 64
    target.manifest.write_text(json.dumps(manifest))
    before = tree(target.instructions.parent)
    with pytest.raises(ValueError, match="unrecognized managed path"):
        native.uninstall(target)
    assert tree(target.instructions.parent) == before


def test_manifest_schema_and_digest_rejected(target):
    native.install(target, Config())
    value = json.loads(target.manifest.read_text())
    value["schema"] = 999
    target.manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="invalid or mismatched"):
        native.status(target)
    value["schema"] = 1
    value["files"]["skill/SKILL.md"] = "bad"
    target.manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="invalid manifest digest"):
        native.uninstall(target)


def test_symlink_parent_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "project"
    root.mkdir()
    try:
        (root / ".codex").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink permission unavailable")
    with pytest.raises(ValueError, match="symlink/reparse"):
        native.install(native.Target.project(root), Config())
    assert not list(outside.iterdir())


def test_directory_in_place_of_file_refused(target):
    target.manifest.mkdir(parents=True)
    with pytest.raises(ValueError, match="regular file"):
        native.status(target)


def test_io_failure_rolls_back_file_changes(target, monkeypatch):
    native.install(target, Config(), automatic=True)
    before = tree(target.instructions.parent)
    real_write = native._write
    count = 0

    def fail_once(path, data, mode=None):
        nonlocal count
        count += 1
        if count == 3:
            raise OSError("simulated disk error")
        return real_write(path, data, mode)

    monkeypatch.setattr(native, "_write", fail_once)
    config = Config.model_validate({"routing": {"max_tier": "luna"}})
    with pytest.raises(OSError, match="disk error"):
        native.install(target, config, automatic=True, update=True)
    assert tree(target.instructions.parent) == before


def test_custom_codex_home_and_user_scope(tmp_path, monkeypatch):
    home, codex = tmp_path / "home", tmp_path / "codex-profile"
    monkeypatch.setenv("CODEX_HOME", str(codex))
    target = native.Target.user(home=home)
    assert target.skill_dir == home / ".agents/skills/jev-intent-router"
    assert target.agent_dir == codex / "agents"
    native.install(target, Config(), automatic=True)
    assert native.status(target)["intact"]
    wrong_target = native.Target.user(home=home, codex_home=tmp_path / "other")
    with pytest.raises(ValueError, match="mismatched"):
        native.uninstall(wrong_target)
    native.uninstall(target)
    assert not target.manifest.exists()


def test_project_manifest_survives_project_move(target, tmp_path):
    native.install(target, Config(), automatic=True)
    destination = tmp_path / "moved"
    target.instructions.parent.rename(destination)
    moved = native.Target.project(destination)
    assert native.status(moved)["intact"]
    native.uninstall(moved)
    assert not tree(destination)


def test_cli_catalog_and_lifecycle(tmp_path, capsys):
    root = str(tmp_path / "target")
    assert main(["catalog"]) == 0
    assert len(json.loads(capsys.readouterr().out)["workers"]) == 12
    assert main(["status", "--project", root]) == 1
    capsys.readouterr()
    assert main(["install", "--project", root, "--auto", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"]
    assert not Path(root).exists()
    assert main(["install", "--project", root, "--auto"]) == 0
    capsys.readouterr()
    assert main(["install", "--project", root, "--update"]) == 0
    assert json.loads(capsys.readouterr().out)["automatic"]
    assert main(["status", "--project", root]) == 0
    capsys.readouterr()
    assert main(["uninstall", "--project", root]) == 0
    capsys.readouterr()


def test_doctor_warns_without_disclosing_url_or_credentials(tmp_path, capsys, monkeypatch):
    root = tmp_path / "project"
    target = native.Target.project(root)
    native.install(target, Config())
    cfg = root / ".codex/config.toml"
    cfg.write_text('model_provider = "jev_router"\nopenai_base_url = "https://secret-token.example"\n[agents]\nenabled = false\n')
    monkeypatch.setenv("CODEX_HOME", str(root / ".codex"))
    assert main(["doctor", "--project", str(root)]) == 0
    out = capsys.readouterr().out
    assert "Non-openai" in out and "disabled" in out and "URL override" in out
    assert "secret-token" not in out


def test_invalid_router_config_does_not_echo_secret(tmp_path, capsys):
    config = tmp_path / "invalid.toml"
    config.write_text('api_key = "sensitive-never-echo"\n')
    assert main(["--config", str(config), "catalog"]) == 2
    output = capsys.readouterr()
    assert "Invalid router configuration" in output.err
    assert "sensitive-never-echo" not in output.err


def test_cli_conflicts_and_invalid_scope(target, capsys):
    target.instructions.parent.mkdir(parents=True)
    with pytest.raises(SystemExit) as error:
        main(["install", "--project", str(target.instructions.parent), "--codex-home", "."])
    assert error.value.code == 2
    capsys.readouterr()
    target.manifest.parent.mkdir(parents=True, exist_ok=True)
    target.manifest.write_text("invalid")
    assert main(["status", "--project", str(target.instructions.parent)]) == 2
    assert "cannot parse" in capsys.readouterr().err


@pytest.mark.parametrize("contents", ['agents = 3\n', '[model_providers]\nopenai = false\n', '[not valid'])
def test_doctor_handles_invalid_config_tables(tmp_path, capsys, monkeypatch, contents):
    root = tmp_path / "target"
    native.install(native.Target.project(root), Config())
    config = root / ".codex/config.toml"
    config.write_text(contents)
    monkeypatch.setenv("CODEX_HOME", str(root / ".codex"))
    assert main(["doctor", "--project", str(root)]) == 0
    assert "Cannot parse" in capsys.readouterr().out


def test_oversized_managed_file_refused(target):
    native.install(target, Config())
    target.manifest.write_bytes(b" " * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="safety limit"):
        native.status(target)


def test_invalid_parent_and_incomplete_manifest(target):
    root = target.instructions.parent
    root.mkdir(parents=True)
    (root / ".agents").write_text("not a directory")
    with pytest.raises(ValueError, match="not a directory"):
        native.install(target, Config())
    (root / ".agents").unlink()
    native.install(target, Config())
    manifest = json.loads(target.manifest.read_text())
    del manifest["files"]["skill/SKILL.md"]
    target.manifest.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="incomplete"):
        native.status(target)


def test_commands_do_not_need_network_or_credentials(target, capsys, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("native installer must not use network or credentials")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(Config, "credentials", forbidden)
    monkeypatch.setenv("CODEX_HOME", str(target.agent_dir.parent))
    for command in (["catalog"], ["install", "--project", str(target.instructions.parent)],
                    ["doctor", "--project", str(target.instructions.parent)],
                    ["uninstall", "--project", str(target.instructions.parent)]):
        assert main(command) == 0
        capsys.readouterr()


def test_source_package_version_and_console_entry():
    import jev_llm_router
    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert project["project"]["version"] == jev_llm_router.__version__
    assert project["project"]["scripts"]["jev-codex"] == "jev_llm_router.codex_cli:main"
    sample = tomllib.loads((root / "examples/codex-native.toml").read_text())
    assert sample["model_provider"] == "openai"
    assert sample["agents"]["max_concurrent_threads_per_session"] == 1
