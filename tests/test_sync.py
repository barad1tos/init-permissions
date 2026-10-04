"""Tests for sync.py: SyncResult, run_sync(), bulk_sync().

Tests cover:
- run_sync on outdated settings (extra allow[] entries) returns UPDATED with correct removed count
- run_sync with dry_run=True on a repo with changes returns WOULD_UPDATE without modifying the file
- run_sync on a repo with matching settings returns UNCHANGED
- run_sync on a non-managed path returns SKIPPED
- run_sync on a repo missing settings.json returns CREATED (generates from scratch)
- bulk_sync calls run_sync for each discovered repo and returns list of SyncResult
- run_sync preserves hooks section in settings.json (only permissions are regenerated)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from init_permissions.models import GeneratedSettings
from init_permissions.sync import (
    BulkSyncPlan,
    Hazard,
    SyncAction,
    SyncResult,
    bulk_sync,
    detect_sync_hazards,
    run_sync,
)

# --- Helpers ---


def _make_managed_repo(tmp_path: Path) -> Path:
    """Create a minimal managed-path repo with .claude/ directory."""
    repo = tmp_path / "git.example.com" / "team" / "test-repo"
    repo.mkdir(parents=True)
    (repo / ".claude").mkdir()
    return repo


def _write_settings(repo: Path, allow: list[str], ask: list[str], extra: dict | None = None) -> None:
    data: dict = {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "permissions": {"allow": allow, "ask": ask},
    }
    if extra:
        data.update(extra)
    (repo / ".claude" / "settings.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


# --- SyncAction enum ---


def test_sync_action_values() -> None:
    """SyncAction enum must have correct string values."""
    assert SyncAction.UNCHANGED.value == "UNCHANGED"
    assert SyncAction.UPDATED.value == "UPDATED"
    assert SyncAction.CREATED.value == "CREATED"
    assert SyncAction.SKIPPED.value == "SKIPPED"
    assert SyncAction.WOULD_UPDATE.value == "WOULD_UPDATE"
    assert SyncAction.WOULD_CREATE.value == "WOULD_CREATE"
    assert SyncAction.ERROR.value == "ERROR"


# --- SyncResult model ---


def test_sync_result_model() -> None:
    """SyncResult must be a Pydantic BaseModel with required fields."""
    result = SyncResult(
        repo_path="/some/path",
        action=SyncAction.UNCHANGED,
        profile="own",
    )
    assert result.entries_removed == 0
    assert result.entries_added == 0
    assert result.error == ""


# --- run_sync: SKIPPED for non-managed path ---


def test_run_sync_skipped_non_managed(tmp_path: Path) -> None:
    """run_sync on a non-managed path (outside every own/work prefix) returns SKIPPED."""
    result = run_sync(tmp_path)

    assert isinstance(result, SyncResult)
    assert result.action == SyncAction.SKIPPED


# --- run_sync: CREATED when settings.json missing ---


def test_run_sync_creates_when_missing(tmp_path: Path) -> None:
    """run_sync on a repo missing settings.json returns CREATED and writes the file."""
    repo = _make_managed_repo(tmp_path)
    # No settings.json file exists

    expected = GeneratedSettings(allow=["entry-a"], ask=[])

    with (
        patch("init_permissions.sync.resolve_profile", return_value=("own", None)),
        patch("init_permissions.sync.generate_settings", return_value=expected),
    ):
        result = run_sync(repo)

    assert result.action == SyncAction.CREATED
    settings_path = repo / ".claude" / "settings.json"
    assert settings_path.exists()


# --- run_sync: WOULD_CREATE in check mode when settings.json missing ---


def test_run_sync_would_create_check_mode(tmp_path: Path) -> None:
    """run_sync with dry_run=True on a repo missing settings.json returns WOULD_CREATE and does NOT write."""
    repo = _make_managed_repo(tmp_path)
    # No settings.json file exists

    expected = GeneratedSettings(allow=["entry-a"], ask=[])

    with (
        patch("init_permissions.sync.resolve_profile", return_value=("own", None)),
        patch("init_permissions.sync.generate_settings", return_value=expected),
    ):
        result = run_sync(repo, dry_run=True)

    assert result.action == SyncAction.WOULD_CREATE
    settings_path = repo / ".claude" / "settings.json"
    assert not settings_path.exists(), "dry_run=True must NOT write files"


# --- run_sync: UNCHANGED when permissions match ---


def test_run_sync_unchanged_when_matching(tmp_path: Path) -> None:
    """run_sync on a repo whose settings.json matches generator output returns UNCHANGED."""
    repo = _make_managed_repo(tmp_path)
    _write_settings(repo, allow=["entry-a", "entry-b"], ask=[])

    expected = GeneratedSettings(allow=["entry-a", "entry-b"], ask=[])

    with (
        patch("init_permissions.sync.resolve_profile", return_value=("own", None)),
        patch("init_permissions.sync.generate_settings", return_value=expected),
    ):
        result = run_sync(repo)

    assert result.action == SyncAction.UNCHANGED
    assert result.entries_removed == 0
    assert result.entries_added == 0


# --- run_sync: UPDATED when permissions differ (extra entries in current) ---


def test_run_sync_updated_with_removed_count(tmp_path: Path) -> None:
    """run_sync on outdated settings with extra allow[] entries returns UPDATED with correct removed count."""
    repo = _make_managed_repo(tmp_path)
    # Current has 3 entries: entry-a, entry-b, extra-old (extra-old is redundant)
    _write_settings(repo, allow=["entry-a", "entry-b", "extra-old"], ask=[])

    # Generator says only entry-a and entry-b are needed
    expected = GeneratedSettings(allow=["entry-a", "entry-b"], ask=[])

    with (
        patch("init_permissions.sync.resolve_profile", return_value=("own", None)),
        patch("init_permissions.sync.generate_settings", return_value=expected),
    ):
        result = run_sync(repo)

    assert result.action == SyncAction.UPDATED
    assert result.entries_removed == 1  # extra-old removed
    assert result.entries_added == 0


# --- run_sync: WOULD_UPDATE in check mode (file not modified) ---


def test_run_sync_would_update_check_mode(tmp_path: Path) -> None:
    """run_sync with dry_run=True on a repo with changes returns WOULD_UPDATE and does NOT modify the file."""
    repo = _make_managed_repo(tmp_path)
    original_content = json.dumps(
        {
            "$schema": "https://json.schemastore.org/claude-code-settings.json",
            "permissions": {"allow": ["entry-a", "extra-old"], "ask": []},
        },
        indent=2,
    )
    (repo / ".claude" / "settings.json").write_text(original_content, encoding="utf-8")

    expected = GeneratedSettings(allow=["entry-a"], ask=[])

    with (
        patch("init_permissions.sync.resolve_profile", return_value=("own", None)),
        patch("init_permissions.sync.generate_settings", return_value=expected),
    ):
        result = run_sync(repo, dry_run=True)

    assert result.action == SyncAction.WOULD_UPDATE
    # File must NOT be modified in check mode
    actual_content = (repo / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert actual_content == original_content


# --- run_sync: preserves hooks section ---


def test_run_sync_preserves_hooks(tmp_path: Path) -> None:
    """run_sync preserves hooks section in settings.json — only permissions are regenerated."""
    repo = _make_managed_repo(tmp_path)
    # Settings.json with hooks section that must be preserved
    original_data = {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "permissions": {"allow": ["entry-a", "extra-old"], "ask": []},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "echo pre"}],
                }
            ]
        },
    }
    (repo / ".claude" / "settings.json").write_text(json.dumps(original_data, indent=2), encoding="utf-8")

    expected = GeneratedSettings(allow=["entry-a"], ask=[])

    with (
        patch("init_permissions.sync.resolve_profile", return_value=("own", None)),
        patch("init_permissions.sync.generate_settings", return_value=expected),
    ):
        result = run_sync(repo)

    assert result.action == SyncAction.UPDATED
    written = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    # hooks section must be preserved exactly
    assert "hooks" in written
    assert written["hooks"] == original_data["hooks"]
    # permissions must be updated
    assert written["permissions"]["allow"] == ["entry-a"]


# --- bulk_sync ---


def _write_global_settings(path: Path, allow: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"permissions": {"allow": allow, "ask": []}}, indent=2),
        encoding="utf-8",
    )


def test_bulk_sync_calls_run_sync_for_each_repo(tmp_path: Path) -> None:
    """bulk_sync calls run_sync for each discovered repo and returns BulkSyncPlan."""
    repo1 = tmp_path / "git.example.com" / "team" / "repo1"
    repo2 = tmp_path / "github.com" / "me" / "repo2"
    for repo in (repo1, repo2):
        repo.mkdir(parents=True)
        (repo / ".claude").mkdir()

    result1 = SyncResult(repo_path=str(repo1), action=SyncAction.UNCHANGED, profile="work-app")
    result2 = SyncResult(repo_path=str(repo2), action=SyncAction.UPDATED, profile="own")

    with (
        patch("init_permissions.sync._discover_managed_repos") as mock_discover,
        patch("init_permissions.sync.load_global_allow", return_value={"g-a", "g-b"}),
        patch("init_permissions.sync.run_sync") as mock_run_sync,
    ):
        mock_discover.return_value = [repo1, repo2]
        mock_run_sync.side_effect = [result1, result2]
        plan = bulk_sync()

    assert isinstance(plan, BulkSyncPlan)
    assert len(plan.results) == 2
    assert all(isinstance(r, SyncResult) for r in plan.results)
    assert plan.hazards == []
    assert plan.excluded == []
    mock_run_sync.assert_any_call(repo1, dry_run=False, global_allow={"g-a", "g-b"})
    mock_run_sync.assert_any_call(repo2, dry_run=False, global_allow={"g-a", "g-b"})


def test_bulk_sync_passes_check_flag(tmp_path: Path) -> None:
    """bulk_sync passes dry_run=True to each run_sync call."""
    repo = tmp_path / "git.example.com" / "team" / "repo1"
    repo.mkdir(parents=True)
    (repo / ".claude").mkdir()

    result = SyncResult(repo_path=str(repo), action=SyncAction.WOULD_UPDATE, profile="work-app")

    with (
        patch("init_permissions.sync._discover_managed_repos") as mock_discover,
        patch("init_permissions.sync.load_global_allow", return_value=set()),
        patch("init_permissions.sync.run_sync") as mock_run_sync,
    ):
        mock_discover.return_value = [repo]
        mock_run_sync.return_value = result
        bulk_sync(dry_run=True)

    mock_run_sync.assert_called_once_with(repo, dry_run=True, global_allow=set())


# --- Hazard detection ---


def test_detect_hazards_flags_self_write_collision(tmp_path: Path) -> None:
    """A repo whose settings.json symlinks to the global file is BLOCKed."""
    global_path = tmp_path / "home" / ".claude" / "settings.json"
    _write_global_settings(global_path, ["keep"])

    repo = tmp_path / "github.com" / "me" / "dotfiles-like"
    (repo / ".claude").mkdir(parents=True)
    symlink_target = repo / ".claude" / "settings.json"
    symlink_target.symlink_to(global_path)

    hazards = detect_sync_hazards([repo], global_settings_path=global_path)

    assert len(hazards) == 1
    assert isinstance(hazards[0], Hazard)
    assert hazards[0].severity == "BLOCK"
    assert hazards[0].category == "self-write-collision"
    assert hazards[0].repo == repo


def test_detect_hazards_flags_dangling_symlink(tmp_path: Path) -> None:
    """A repo with a dangling settings.json symlink emits a WARN hazard."""
    global_path = tmp_path / "home" / ".claude" / "settings.json"
    _write_global_settings(global_path, ["keep"])

    repo = tmp_path / "git.example.com" / "team" / "broken"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").symlink_to(tmp_path / "does-not-exist.json")

    hazards = detect_sync_hazards([repo], global_settings_path=global_path)

    assert [h.severity for h in hazards] == ["WARN"]
    assert hazards[0].category == "dangling-symlink"


def test_detect_hazards_clean_plan(tmp_path: Path) -> None:
    """A plan with no symlinks and no collisions returns an empty hazard list."""
    global_path = tmp_path / "home" / ".claude" / "settings.json"
    _write_global_settings(global_path, ["keep"])

    repo = tmp_path / "github.com" / "me" / "regular"
    (repo / ".claude").mkdir(parents=True)
    _write_settings(repo, allow=["entry-a"], ask=[])

    assert detect_sync_hazards([repo], global_settings_path=global_path) == []


# --- Snapshot + exclusion integration ---


def test_bulk_sync_excludes_self_write_repo(tmp_path: Path) -> None:
    """bulk_sync excludes a repo whose settings.json resolves to the global source."""
    global_path = tmp_path / "home" / ".claude" / "settings.json"
    _write_global_settings(global_path, ["keep"])

    safe_repo = tmp_path / "git.example.com" / "team" / "safe"
    (safe_repo / ".claude").mkdir(parents=True)
    _write_settings(safe_repo, allow=["entry-a"], ask=[])

    dangerous_repo = tmp_path / "github.com" / "me" / "dotfiles"
    (dangerous_repo / ".claude").mkdir(parents=True)
    (dangerous_repo / ".claude" / "settings.json").symlink_to(global_path)

    safe_result = SyncResult(repo_path=str(safe_repo), action=SyncAction.UNCHANGED, profile="work-app")

    with (
        patch("init_permissions.sync._discover_managed_repos", return_value=[safe_repo, dangerous_repo]),
        patch("init_permissions.sync._GLOBAL_SETTINGS_PATH", global_path),
        patch("init_permissions.sync.load_global_allow", return_value={"keep"}),
        patch("init_permissions.sync.run_sync") as mock_run_sync,
    ):
        mock_run_sync.return_value = safe_result
        plan = bulk_sync()

    assert [Path(r.repo_path).name for r in plan.results] == ["safe"]
    assert plan.excluded == [dangerous_repo]
    assert [h.category for h in plan.hazards] == ["self-write-collision"]
    # run_sync called only for the safe repo — dangerous one must be filtered out
    mock_run_sync.assert_called_once_with(safe_repo, dry_run=False, global_allow={"keep"})


def test_bulk_sync_snapshots_global_allow_once(tmp_path: Path) -> None:
    """bulk_sync calls load_global_allow exactly once, not once per repo.

    This prevents the 2026-04-07 cascade bug: if dotfiles writes the global
    file mid-loop, subsequent repos must still see the original snapshot.
    """
    repo1 = tmp_path / "git.example.com" / "team" / "a"
    repo2 = tmp_path / "github.com" / "me" / "b"
    for r in (repo1, repo2):
        (r / ".claude").mkdir(parents=True)

    result = SyncResult(repo_path="", action=SyncAction.UNCHANGED, profile="")
    with (
        patch("init_permissions.sync._discover_managed_repos", return_value=[repo1, repo2]),
        patch("init_permissions.sync.load_global_allow", return_value={"snap"}) as mock_load,
        patch("init_permissions.sync.run_sync", return_value=result) as mock_run,
    ):
        bulk_sync()

    assert mock_load.call_count == 1
    # Every run_sync call received the same snapshot object
    for call in mock_run.call_args_list:
        assert call.kwargs["global_allow"] == {"snap"}
