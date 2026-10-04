"""Tests for layout.py: config.toml decides which checkouts are managed and how."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from init_permissions.checker import run_check
from init_permissions.detector import detect_profile
from init_permissions.generator import write_settings
from init_permissions.layout import CONFIG_ENV_VAR, classify, load_layout, settings_relpath
from init_permissions.models import GeneratedSettings
from init_permissions.sync import SyncAction, run_sync
from init_permissions.validator import _discover_managed_repos


def _write_config(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_no_config_manages_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without config.toml even a repo missing settings.json is left alone."""
    monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "absent.toml"))
    repo = tmp_path / "github.com" / "me" / "repo"
    repo.mkdir(parents=True)

    assert classify(repo) is None
    assert run_check(repo).warnings == []
    assert _discover_managed_repos() == []


def test_longest_prefix_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An employer's GitHub org listed under work overrides the broader own prefix."""
    config = _write_config(
        tmp_path / "config.toml",
        f'git_root = "{tmp_path}"\n[profiles]\nown = ["github.com"]\nwork = ["github.com/acme"]\n',
    )
    monkeypatch.setenv(CONFIG_ENV_VAR, str(config))

    assert classify(tmp_path / "github.com" / "me" / "tool") == "own"
    assert classify(tmp_path / "github.com" / "acme" / "service") == "work"


def test_prefix_directory_and_worktree(git_layout: Path) -> None:
    """Only paths strictly below a prefix match; a worktree inside a repo inherits its family."""
    assert classify(git_layout / "github.com" / "me") == "own"
    assert classify(git_layout / "github.com") is None
    worktree = git_layout / "git.example.com" / "team" / "infra" / ".worktrees" / "fix"
    assert classify(worktree) == "work"


def test_work_repo_with_terraform_is_infra(git_layout: Path) -> None:
    repo = git_layout / "git.example.com" / "team" / "network"
    repo.mkdir(parents=True)
    (repo / "main.tf").write_text("", encoding="utf-8")

    assert detect_profile(repo) == "work-infra"


def test_git_root_expands_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = _write_config(tmp_path / "config.toml", 'git_root = "~/git"\n[profiles]\nown = ["github.com"]\n')
    monkeypatch.setenv(CONFIG_ENV_VAR, str(config))

    assert classify(tmp_path / "git" / "github.com" / "me" / "repo") == "own"


def test_invalid_config_names_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _write_config(tmp_path / "config.toml", '[profiles]\nown = "github.com"\n')
    monkeypatch.setenv(CONFIG_ENV_VAR, str(config))

    with pytest.raises(ValueError, match="config.toml"):
        load_layout()


def test_discovery_spans_hosts_without_duplicates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Overlapping prefixes still list each repo once; repos without .claude/ are not discovered."""
    config = _write_config(
        tmp_path / "config.toml",
        f'git_root = "{tmp_path}"\n[profiles]\nown = ["github.com"]\nwork = ["github.com/acme", "git.example.com"]\n',
    )
    monkeypatch.setenv(CONFIG_ENV_VAR, str(config))
    expected = [
        tmp_path / "git.example.com" / "team" / "infra",
        tmp_path / "github.com" / "acme" / "service",
        tmp_path / "github.com" / "me" / "tool",
    ]
    for repo in expected:
        (repo / ".claude").mkdir(parents=True)
    (tmp_path / "github.com" / "me" / "no-claude").mkdir()

    assert _discover_managed_repos() == sorted(expected)


def test_work_repo_profile_goes_to_local_layer(git_layout: Path) -> None:
    """Sync never touches a team-owned settings.json; the profile lands in settings.local.json."""
    repo = git_layout / "git.example.com" / "team" / "service"
    (repo / ".claude").mkdir(parents=True)
    team_settings = repo / ".claude" / "settings.json"
    team_bytes = json.dumps({"permissions": {"allow": [], "deny": ["Read(./.env)"]}}) + "\n"
    team_settings.write_text(team_bytes, encoding="utf-8")

    result = run_sync(repo, global_allow=set())

    assert result.action == SyncAction.CREATED
    assert team_settings.read_text(encoding="utf-8") == team_bytes
    local = json.loads((repo / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert "Bash(git push:*)" in local["permissions"]["ask"]
    assert run_check(repo).warnings == []


def test_write_settings_keeps_other_local_keys(git_layout: Path) -> None:
    """Personal MCP settings in settings.local.json survive a regenerate."""
    repo = git_layout / "git.example.com" / "team" / "workspace"
    (repo / ".claude").mkdir(parents=True)
    local = repo / ".claude" / "settings.local.json"
    local.write_text(json.dumps({"enabledMcpjsonServers": ["jira"]}), encoding="utf-8")

    write_settings(GeneratedSettings(allow=["Bash(make:*)"], ask=["Bash(git push:*)"]), repo)

    written = json.loads(local.read_text(encoding="utf-8"))
    assert written["enabledMcpjsonServers"] == ["jira"]
    assert written["permissions"]["ask"] == ["Bash(git push:*)"]


def test_own_repo_keeps_shared_settings(git_layout: Path) -> None:
    assert settings_relpath(git_layout / "github.com" / "me" / "tool") == Path(".claude/settings.json")
