"""Tests for init_permissions.global_sync.

Every test uses tmp_path — never touches the real ~/.claude/settings.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from init_permissions.cli import cli
from init_permissions.global_sync import (
    run_global_check,
    run_global_sync,
    run_global_validate,
)
from init_permissions.profiles import GLOBAL
from init_permissions.sync import SyncAction
from init_permissions.validator import Verdict


@pytest.fixture
def global_path(tmp_path: Path) -> Path:
    """Isolated global settings path."""
    return tmp_path / ".claude" / "settings.json"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


_BASELINE_ALLOW = [e for e in GLOBAL.allow if e != "Read(~/.claude/**)"]


# --- run_global_sync --------------------------------------------------------


def test_global_sync_creates_when_missing(global_path: Path) -> None:
    result = run_global_sync(global_path)
    assert result.action == SyncAction.CREATED
    assert global_path.exists()
    data = json.loads(global_path.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == sorted(GLOBAL.allow)
    assert "ask" not in data["permissions"]


def test_global_sync_would_create_dry_run(global_path: Path) -> None:
    result = run_global_sync(global_path, dry_run=True)
    assert result.action == SyncAction.WOULD_CREATE
    assert not global_path.exists()


def test_global_sync_preserves_hooks_and_enabled_plugins(global_path: Path) -> None:
    hooks_block = {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}
    plugins_block = {"some-plugin": True}
    _write_json(
        global_path,
        {
            "$schema": "https://example.com/old",
            "hooks": hooks_block,
            "enabledPlugins": plugins_block,
            "permissions": {"allow": ["Bash(npm:*)"]},
        },
    )
    result = run_global_sync(global_path)
    assert result.action == SyncAction.UPDATED

    data = json.loads(global_path.read_text(encoding="utf-8"))
    assert data["hooks"] == hooks_block
    assert data["enabledPlugins"] == plugins_block
    assert data["permissions"]["allow"] == sorted(GLOBAL.allow)


def test_global_sync_idempotent(global_path: Path) -> None:
    run_global_sync(global_path)
    second = run_global_sync(global_path)
    assert second.action == SyncAction.UNCHANGED


def test_global_sync_unchanged_when_matching(global_path: Path) -> None:
    _write_json(
        global_path,
        {
            "$schema": "https://json.schemastore.org/claude-code-settings.json",
            "permissions": {"allow": sorted(GLOBAL.allow)},
        },
    )
    result = run_global_sync(global_path)
    assert result.action == SyncAction.UNCHANGED
    assert result.entries_removed == 0
    assert result.entries_added == 0


def test_global_sync_would_update_dry_run(global_path: Path) -> None:
    _write_json(
        global_path,
        {"$schema": "x", "permissions": {"allow": ["Bash(stale:*)"]}},
    )
    original_bytes = global_path.read_bytes()
    result = run_global_sync(global_path, dry_run=True)
    assert result.action == SyncAction.WOULD_UPDATE
    assert global_path.read_bytes() == original_bytes


def test_global_sync_merges_existing_baseline(global_path: Path) -> None:
    _write_json(
        global_path,
        {"$schema": "x", "permissions": {"allow": sorted(_BASELINE_ALLOW)}},
    )
    result = run_global_sync(global_path)
    assert result.action == SyncAction.UPDATED
    assert result.entries_removed == 0
    assert result.entries_added == 1
    data = json.loads(global_path.read_text(encoding="utf-8"))
    allow = data["permissions"]["allow"]
    for entry in _BASELINE_ALLOW:
        assert entry in allow
    assert "Read(~/.claude/**)" in allow


def test_global_sync_atomic_write(global_path: Path) -> None:
    run_global_sync(global_path)
    # File exists and parses as valid JSON
    data = json.loads(global_path.read_text(encoding="utf-8"))
    assert "permissions" in data


# --- run_global_check -------------------------------------------------------


def test_global_check_silent_when_clean(global_path: Path) -> None:
    run_global_sync(global_path)
    assert run_global_check(global_path) == []


def test_global_check_warns_on_drift(global_path: Path) -> None:
    _write_json(
        global_path,
        {"$schema": "x", "permissions": {"allow": sorted(_BASELINE_ALLOW)}},
    )
    warnings = run_global_check(global_path)
    assert warnings
    assert any("DRIFT" in w for w in warnings)


def test_global_check_missing_file(global_path: Path) -> None:
    warnings = run_global_check(global_path)
    assert warnings
    assert any("MISSING" in w for w in warnings)


# --- run_global_validate ----------------------------------------------------


def test_global_validate_pass(global_path: Path) -> None:
    run_global_sync(global_path)
    result = run_global_validate(global_path)
    assert result.verdict == Verdict.PASS


def test_global_validate_warn_on_extras(global_path: Path) -> None:
    extra_allow = sorted([*GLOBAL.allow, "Bash(extra-tool:*)"])
    _write_json(global_path, {"$schema": "x", "permissions": {"allow": extra_allow}})
    result = run_global_validate(global_path)
    assert result.verdict == Verdict.WARN


def test_global_validate_fail_on_missing_allow(global_path: Path) -> None:
    _write_json(global_path, {"$schema": "x", "permissions": {"allow": sorted(_BASELINE_ALLOW)}})
    result = run_global_validate(global_path)
    assert result.verdict == Verdict.FAIL


def test_global_validate_fail_on_ask_drift(global_path: Path) -> None:
    _write_json(
        global_path,
        {
            "$schema": "x",
            "permissions": {
                "allow": sorted(GLOBAL.allow),
                "ask": ["Bash(rm -rf /:*)"],
            },
        },
    )
    result = run_global_validate(global_path)
    assert result.verdict == Verdict.FAIL


# --- CLI smoke (Task 3 will populate the cli; placeholders kept here for cohesion)


def test_cli_global_sync_creates(tmp_path: Path) -> None:
    target = tmp_path / ".claude" / "settings.json"
    runner = CliRunner()
    result = runner.invoke(cli, ["global", "sync", "--global-path", str(target)])
    assert result.exit_code == 0, result.output
    assert target.exists()


def test_cli_global_sync_check_dry_run(tmp_path: Path) -> None:
    target = tmp_path / ".claude" / "settings.json"
    runner = CliRunner()
    result = runner.invoke(cli, ["global", "sync", "--check", "--global-path", str(target)])
    assert result.exit_code == 0, result.output
    assert not target.exists()


def test_cli_global_check_silent_when_clean(tmp_path: Path) -> None:
    target = tmp_path / ".claude" / "settings.json"
    runner = CliRunner()
    runner.invoke(cli, ["global", "sync", "--global-path", str(target)])
    result = runner.invoke(cli, ["global", "check", "--global-path", str(target)])
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_cli_global_validate_pass(tmp_path: Path) -> None:
    target = tmp_path / ".claude" / "settings.json"
    runner = CliRunner()
    runner.invoke(cli, ["global", "sync", "--global-path", str(target)])
    result = runner.invoke(cli, ["global", "validate", "--global-path", str(target)])
    assert result.exit_code == 0
    assert "PASS" in result.output
