"""Tests for checker.py: run_check() DET-01, DET-02, DET-03, and path-scoping.

Tests cover:
- Non-managed paths are skipped (no output)
- Missing settings.json triggers DET-01/02 warning
- Clean repo with correct settings produces no warning
- Drift in ask[] triggers DET-03 warning
- Missing entries in allow[] triggers DET-03 warning
- Extra entries in allow[] beyond expected are accepted (superset is OK)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from init_permissions.checker import CheckResult, run_check
from init_permissions.models import GeneratedSettings

# --- Path scoping tests ---


def test_non_developer_path_skipped(tmp_path: Path) -> None:
    """Non-Developer paths (e.g. /tmp/foo) must be skipped — no warnings returned."""
    # tmp_path is something like /private/var/folders/... — not a Developer path
    result = run_check(tmp_path)
    assert isinstance(result, CheckResult)
    assert result.warnings == []


def test_home_claude_path_skipped(tmp_path: Path) -> None:
    """~/.claude path (global workspace) must be skipped — not a managed repo."""
    # Simulate a path that doesn't contain /Developer/Work/ or /Developer/Own/
    non_managed = tmp_path / "home" / ".claude"
    non_managed.mkdir(parents=True)
    result = run_check(non_managed)
    assert result.warnings == []


# --- DET-01 / DET-02: Missing settings.json ---


def test_missing_settings_returns_warning(tmp_path: Path) -> None:
    """DET-01: Managed path missing .claude/settings.json produces warning with fix command."""
    # Create a managed path by embedding /Developer/Work/ in the structure
    managed_repo = tmp_path / "Developer" / "Work" / "test-repo"
    managed_repo.mkdir(parents=True)
    # No .claude/settings.json created — this is the missing-settings case

    result = run_check(managed_repo)
    assert len(result.warnings) >= 1
    warning_text = " ".join(result.warnings)
    assert "MISSING" in warning_text
    assert "settings.json" in warning_text
    assert "FIX:" in warning_text
    assert "CHECK:" in warning_text


def test_new_managed_repo_prompts_setup(tmp_path: Path) -> None:
    """DET-02: Managed path without .claude/ directory at all still triggers setup warning."""
    managed_repo = tmp_path / "Developer" / "Own" / "new-repo"
    managed_repo.mkdir(parents=True)
    # No .claude/ directory at all — completely new repo

    result = run_check(managed_repo)
    assert len(result.warnings) >= 1
    warning_text = " ".join(result.warnings)
    assert "MISSING" in warning_text
    assert "FIX:" in warning_text


# --- DET-03: Drift detection ---


def _make_managed_repo(tmp_path: Path, profile_segment: str = "Work") -> Path:
    """Helper: create a managed repo directory with .claude/ sub-directory."""
    repo = tmp_path / "Developer" / profile_segment / "drift-test-repo"
    repo.mkdir(parents=True)
    claude_dir = repo / ".claude"
    claude_dir.mkdir()
    return repo


def _write_settings(repo: Path, allow: list[str], ask: list[str]) -> None:
    """Helper: write a minimal settings.json to the repo's .claude/ directory."""
    data = {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "permissions": {"allow": allow, "ask": ask},
    }
    (repo / ".claude" / "settings.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def test_clean_repo_no_warning(tmp_path: Path) -> None:
    """DET-03: Repo whose settings match expected profile produces no warnings."""
    repo = _make_managed_repo(tmp_path)
    # Write settings that match what the mock will return as expected
    _write_settings(repo, allow=["entry-a", "entry-b"], ask=["ask-a"])

    expected = GeneratedSettings(allow=["entry-a", "entry-b"], ask=["ask-a"])

    with (
        patch("init_permissions.checker.resolve_profile", return_value=("work-app", None)),
        patch("init_permissions.checker.generate_settings", return_value=expected),
    ):
        result = run_check(repo)

    assert result.warnings == []


def test_ask_drift_detected(tmp_path: Path) -> None:
    """DET-03: Extra entry in actual ask[] (beyond expected) triggers drift warning."""
    repo = _make_managed_repo(tmp_path)
    # Actual settings has an extra ask entry not in expected
    _write_settings(
        repo, allow=["entry-a", "entry-b"], ask=["ask-a", "extra-ask-entry"]
    )

    expected = GeneratedSettings(allow=["entry-a", "entry-b"], ask=["ask-a"])

    with (
        patch("init_permissions.checker.resolve_profile", return_value=("work-app", None)),
        patch("init_permissions.checker.generate_settings", return_value=expected),
    ):
        result = run_check(repo)

    assert len(result.warnings) >= 1
    warning_text = " ".join(result.warnings)
    assert "ask[]" in warning_text
    assert "extra-ask-entry" in warning_text


def test_allow_missing_detected(tmp_path: Path) -> None:
    """DET-03: Profile allow[] entry absent from actual settings triggers drift warning."""
    repo = _make_managed_repo(tmp_path)
    # Actual settings is missing entry-b from expected allow
    _write_settings(repo, allow=["entry-a"], ask=["ask-a"])

    expected = GeneratedSettings(allow=["entry-a", "entry-b"], ask=["ask-a"])

    with (
        patch("init_permissions.checker.resolve_profile", return_value=("work-app", None)),
        patch("init_permissions.checker.generate_settings", return_value=expected),
    ):
        result = run_check(repo)

    assert len(result.warnings) >= 1
    warning_text = " ".join(result.warnings)
    assert "allow[]" in warning_text
    assert "entry-b" in warning_text


def test_allow_superset_accepted(tmp_path: Path) -> None:
    """DET-03: Extra entries in actual allow[] beyond expected are acceptable (D-06)."""
    repo = _make_managed_repo(tmp_path)
    # Actual has extra-allow-entry beyond what expected profile requires — this is OK
    _write_settings(
        repo,
        allow=["entry-a", "entry-b", "extra-allow-entry"],
        ask=["ask-a"],
    )

    expected = GeneratedSettings(allow=["entry-a", "entry-b"], ask=["ask-a"])

    with (
        patch("init_permissions.checker.resolve_profile", return_value=("work-app", None)),
        patch("init_permissions.checker.generate_settings", return_value=expected),
    ):
        result = run_check(repo)

    assert result.warnings == []
