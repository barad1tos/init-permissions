"""Tests for settings.json generator: schema validation, snapshot-style, and merge logic.

Covers Layer 1 (schema) and Layer 2 (snapshot-style) of D-17.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from init_permissions.generator import (
    generate_settings,
    load_repo_config,
    resolve_profile,
    write_settings,
)
from init_permissions.models import RepoConfig


def _generate(profile_name: str, tmp_path: Path, repo_config: RepoConfig | None = None) -> dict:
    """Helper: generate settings with no global allow (avoids dedup surprises in tests)."""
    with patch("init_permissions.generator.load_global_allow", return_value=set()):
        settings = generate_settings(profile_name, tmp_path, repo_config)
    return settings.to_settings_dict()


# --- Layer 1: Schema validation ---


def test_generated_output_has_schema(tmp_path: Path) -> None:
    """Generated output must include a $schema URL."""
    output = _generate("work-infra", tmp_path)
    assert "$schema" in output


def test_generated_output_has_permissions_allow(tmp_path: Path) -> None:
    """Generated output must have permissions.allow as a list."""
    output = _generate("work-infra", tmp_path)
    assert "permissions" in output
    assert isinstance(output["permissions"]["allow"], list)


def test_generated_output_no_deny(tmp_path: Path) -> None:
    """D-13: generated output must NOT have a deny key in permissions."""
    for profile_name in ("work-infra", "work-app", "own"):
        output = _generate(profile_name, tmp_path)
        assert "deny" not in output.get("permissions", {}), f"Profile '{profile_name}' unexpectedly has deny in generated output"


# --- Layer 2: Snapshot-style tests ---


def test_work_infra_has_terraform_entries(tmp_path: Path) -> None:
    """Work-infra profile generates terraform ask entries."""
    output = _generate("work-infra", tmp_path)
    ask = output["permissions"].get("ask", [])
    assert "Bash(terraform apply:*)" in ask
    assert "Bash(terraform import:*)" in ask


def test_work_app_with_node_stack(tmp_path: Path) -> None:
    """Work-app with package.json present includes npm entries in allow."""
    (tmp_path / "package.json").write_text('{"name": "app"}')
    output = _generate("work-app", tmp_path)
    allow = output["permissions"]["allow"]
    assert "Bash(npm:*)" in allow


def test_own_empty_ask(tmp_path: Path) -> None:
    """D-22: Own profile generates empty ask[] (key absent or empty list)."""
    output = _generate("own", tmp_path)
    ask = output["permissions"].get("ask", [])
    assert ask == []


def test_own_ask_key_absent_when_empty(tmp_path: Path) -> None:
    """When ask[] is empty, to_settings_dict should not include the ask key."""
    output = _generate("own", tmp_path)
    # The own profile has empty ask, so the key should be absent from output
    assert "ask" not in output.get("permissions", {})


def test_repo_config_merge(tmp_path: Path) -> None:
    """RepoConfig.webfetch_domains entries are converted to WebFetch(domain:X) in allow."""
    repo_config = RepoConfig(webfetch_domains=["example.com"])
    output = _generate("work-app", tmp_path, repo_config)
    allow = output["permissions"]["allow"]
    assert "WebFetch(domain:example.com)" in allow


def test_repo_config_merge_mcp_tools(tmp_path: Path) -> None:
    """RepoConfig.mcp_tools entries are added directly to allow."""
    repo_config = RepoConfig(mcp_tools=["mcp__test__tool"])
    output = _generate("work-app", tmp_path, repo_config)
    allow = output["permissions"]["allow"]
    assert "mcp__test__tool" in allow


def test_repo_config_merge_bash_commands(tmp_path: Path) -> None:
    """RepoConfig.bash_commands entries are added directly to allow."""
    repo_config = RepoConfig(bash_commands=["Bash(./custom-script.sh:*)"])
    output = _generate("work-app", tmp_path, repo_config)
    allow = output["permissions"]["allow"]
    assert "Bash(./custom-script.sh:*)" in allow


def test_global_deduplication(tmp_path: Path) -> None:
    """Entries already in global allow[] are removed from generated output."""
    # Simulate global settings containing an entry that work-infra profile also has
    global_entries = {"Bash(terraform force-unlock:*)"}

    with patch("init_permissions.generator.load_global_allow", return_value=global_entries):
        settings = generate_settings("work-infra", tmp_path)
    output = settings.to_settings_dict()
    allow = output["permissions"]["allow"]

    assert "Bash(terraform force-unlock:*)" not in allow


def test_write_settings(tmp_path: Path) -> None:
    """write_settings creates .claude/settings.json with valid JSON structure."""
    with patch("init_permissions.generator.load_global_allow", return_value=set()):
        settings = generate_settings("work-app", tmp_path)

    written_path = write_settings(settings, tmp_path)

    assert written_path.exists()
    assert written_path == tmp_path / ".claude" / "settings.json"

    content = json.loads(written_path.read_text(encoding="utf-8"))
    assert "$schema" in content
    assert "permissions" in content
    assert isinstance(content["permissions"]["allow"], list)


def test_write_settings_creates_claude_dir(tmp_path: Path) -> None:
    """write_settings creates .claude/ directory if it does not exist."""
    claude_dir = tmp_path / ".claude"
    assert not claude_dir.exists()

    with patch("init_permissions.generator.load_global_allow", return_value=set()):
        settings = generate_settings("own", tmp_path)
    write_settings(settings, tmp_path)

    assert claude_dir.exists()
    assert (claude_dir / "settings.json").exists()


def test_load_repo_config_returns_none_when_absent(tmp_path: Path) -> None:
    """load_repo_config returns None when .claude/repo-config.json does not exist."""
    result = load_repo_config(tmp_path)
    assert result is None


def test_load_repo_config_parses_file(tmp_path: Path) -> None:
    """load_repo_config correctly parses .claude/repo-config.json when present."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    config_data = {
        "webfetch_domains": ["api.example.com"],
        "mcp_tools": ["mcp__sentry__list_issues"],
        "bash_commands": [],
        "skills": [],
    }
    (claude_dir / "repo-config.json").write_text(json.dumps(config_data))

    result = load_repo_config(tmp_path)
    assert result is not None
    assert result.webfetch_domains == ["api.example.com"]
    assert result.mcp_tools == ["mcp__sentry__list_issues"]


# --- resolve_profile + persistent profile override ---


def _write_repo_config(repo_path: Path, data: dict) -> None:
    claude_dir = repo_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "repo-config.json").write_text(json.dumps(data))


def test_resolve_profile_no_repo_config_falls_back_to_detection(tmp_path: Path) -> None:
    """When no repo-config.json is present, resolve_profile uses path detection."""
    with patch("init_permissions.generator.detect_profile", return_value="work-app"):
        profile, repo_config = resolve_profile(tmp_path)
    assert profile == "work-app"
    assert repo_config is None


def test_resolve_profile_repo_config_without_profile_falls_back(tmp_path: Path) -> None:
    """A repo-config.json without `profile` key still defers to path detection."""
    _write_repo_config(tmp_path, {"webfetch_domains": ["api.example.com"]})
    with patch("init_permissions.generator.detect_profile", return_value="work-infra"):
        profile, repo_config = resolve_profile(tmp_path)
    assert profile == "work-infra"
    assert repo_config is not None
    assert repo_config.profile is None


def test_resolve_profile_override_wins_over_detection(tmp_path: Path) -> None:
    """A repo-config.json `profile` override takes precedence over path detection."""
    _write_repo_config(tmp_path, {"profile": "work-app"})
    with patch("init_permissions.generator.detect_profile", return_value="work-infra"):
        profile, repo_config = resolve_profile(tmp_path)
    assert profile == "work-app"
    assert repo_config is not None
    assert repo_config.profile == "work-app"


def test_repo_config_rejects_invalid_profile_value() -> None:
    """RepoConfig.profile only accepts the three known profile names."""
    with pytest.raises(ValidationError):
        RepoConfig.model_validate({"profile": "garbage"})
