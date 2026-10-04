"""Tests for validator.py: ValidationResult, run_validate(), bulk_validate().

Tests cover:
- Clean repo returns PASS with profile name
- Missing settings.json returns FAIL
- ask[] mismatch returns FAIL with details
- Extra allow[] entries return WARN (harmless customization)
- Missing expected allow[] entries return FAIL
- Profile mismatch detection (ask[] signature doesn't match detected profile)
- Non-managed paths return PASS with empty profile
- bulk_validate() discovers all managed repos and returns per-repo results
- bulk_validate() handles mixed clean and drifted repos correctly
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from init_permissions.models import GeneratedSettings
from init_permissions.validator import (
    ValidationResult,
    Verdict,
    bulk_validate,
    run_validate,
)

# --- Helpers ---


def _make_managed_repo(tmp_path: Path, segment: str = "Work") -> Path:
    """Create a minimal managed-path repo with .claude/ directory."""
    repo = tmp_path / "Developer" / segment / "test-repo"
    repo.mkdir(parents=True)
    (repo / ".claude").mkdir()
    return repo


def _write_settings(repo: Path, allow: list[str], ask: list[str]) -> None:
    data = {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "permissions": {"allow": allow, "ask": ask},
    }
    (repo / ".claude" / "settings.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


# --- Verdict enum ---


def test_verdict_values() -> None:
    """Verdict enum must have string values for clean output."""
    assert Verdict.PASS.value == "PASS"
    assert Verdict.WARN.value == "WARN"
    assert Verdict.FAIL.value == "FAIL"


# --- Non-managed path ---


def test_non_managed_path_returns_pass(tmp_path: Path) -> None:
    """Non-managed path (e.g. /tmp/foo) must return PASS with empty profile."""
    # tmp_path won't contain /Developer/Work/ or /Developer/Own/
    result = run_validate(tmp_path)

    assert isinstance(result, ValidationResult)
    assert result.verdict == Verdict.PASS
    assert result.profile == ""
    assert result.issues == []


# --- Missing settings.json ---


def test_missing_settings_returns_fail(tmp_path: Path) -> None:
    """Managed repo without settings.json returns FAIL."""
    repo = tmp_path / "Developer" / "Work" / "no-settings-repo"
    repo.mkdir(parents=True)
    # No .claude/settings.json

    result = run_validate(repo)

    assert result.verdict == Verdict.FAIL
    assert len(result.issues) >= 1


# --- Clean repo ---


def test_clean_repo_returns_pass(tmp_path: Path) -> None:
    """Clean repo with matching settings returns PASS and profile name."""
    repo = _make_managed_repo(tmp_path)
    _write_settings(repo, allow=["entry-a", "entry-b"], ask=["ask-a"])

    expected = GeneratedSettings(allow=["entry-a", "entry-b"], ask=["ask-a"])
    # For clean repo: detected profile matches applied profile
    with (
        patch("init_permissions.validator.resolve_profile", return_value=("work-app", None)),
        patch("init_permissions.validator.run_check") as mock_check,
        patch(
            "init_permissions.validator.identify_applied_profile",
            return_value="work-app",
        ),
        patch("init_permissions.validator.generate_settings", return_value=expected),
    ):
        from init_permissions.checker import CheckResult

        mock_check.return_value = CheckResult(repo_path=str(repo), profile="work-app", warnings=[])
        result = run_validate(repo)

    assert result.verdict == Verdict.PASS
    assert result.profile == "work-app"
    assert result.issues == []
    assert result.profile_mismatch is False


# --- ask[] mismatch ---


def test_ask_mismatch_returns_fail(tmp_path: Path) -> None:
    """Repo with wrong ask[] entries returns FAIL with specific issue details."""
    repo = _make_managed_repo(tmp_path)
    _write_settings(repo, allow=["entry-a"], ask=["ask-a", "rogue-ask"])

    with (
        patch("init_permissions.validator.resolve_profile", return_value=("work-app", None)),
        patch("init_permissions.validator.run_check") as mock_check,
        patch(
            "init_permissions.validator.identify_applied_profile",
            return_value="work-app",
        ),
    ):
        from init_permissions.checker import CheckResult

        mock_check.return_value = CheckResult(
            repo_path=str(repo),
            profile="work-app",
            warnings=["DRIFT: ask[] mismatch — extra: ['rogue-ask'], missing: []"],
        )
        result = run_validate(repo)

    assert result.verdict == Verdict.FAIL
    assert any("ask[]" in issue for issue in result.issues)


# --- Extra allow[] entries (WARN) ---


def test_allow_extra_returns_warn(tmp_path: Path) -> None:
    """Repo with extra allow[] entries beyond profile returns WARN, not FAIL."""
    repo = _make_managed_repo(tmp_path)
    _write_settings(repo, allow=["entry-a", "entry-b", "extra-custom-entry"], ask=["ask-a"])

    # expected only has entry-a + entry-b; extra-custom-entry is the addition
    expected = GeneratedSettings(allow=["entry-a", "entry-b"], ask=["ask-a"])

    # run_check returns no warnings (superset is acceptable per D-06/D-09)
    # But validator detects extra allow[] entries and promotes to WARN.
    with (
        patch("init_permissions.validator.resolve_profile", return_value=("work-app", None)),
        patch("init_permissions.validator.run_check") as mock_check,
        patch(
            "init_permissions.validator.identify_applied_profile",
            return_value="work-app",
        ),
        patch("init_permissions.validator.generate_settings", return_value=expected),
    ):
        from init_permissions.checker import CheckResult

        mock_check.return_value = CheckResult(repo_path=str(repo), profile="work-app", warnings=[])
        result = run_validate(repo)

    # Extra allow[] is harmless customization — WARN not FAIL
    assert result.verdict == Verdict.WARN
    assert any("extra" in issue for issue in result.issues)


# --- Missing expected allow[] entries ---


def test_allow_missing_returns_fail(tmp_path: Path) -> None:
    """Repo missing expected allow[] entries returns FAIL."""
    repo = _make_managed_repo(tmp_path)
    _write_settings(repo, allow=["entry-a"], ask=["ask-a"])  # missing entry-b

    with (
        patch("init_permissions.validator.resolve_profile", return_value=("work-app", None)),
        patch("init_permissions.validator.run_check") as mock_check,
        patch(
            "init_permissions.validator.identify_applied_profile",
            return_value="work-app",
        ),
    ):
        from init_permissions.checker import CheckResult

        mock_check.return_value = CheckResult(
            repo_path=str(repo),
            profile="work-app",
            warnings=["DRIFT: allow[] missing 1 expected entries: ['entry-b']"],
        )
        result = run_validate(repo)

    assert result.verdict == Verdict.FAIL
    assert any("allow[]" in issue for issue in result.issues)


# --- Profile mismatch detection (VALID-03) ---


def test_profile_mismatch_detected(tmp_path: Path) -> None:
    """Repo with ask[] signature not matching detected profile is flagged as mismatch."""
    repo = _make_managed_repo(tmp_path)
    # Write settings as if work-infra was applied (11 ask[]) but path says work-app
    infra_ask = [
        "Bash(git push:*)",
        "Bash(terraform apply:*)",
        "Bash(terraform import:*)",
        "Bash(docker build:*)",
        "Bash(docker push:*)",
        "Bash(kubectl apply:*)",
        "Bash(kubectl delete:*)",
        "Bash(kubectl patch:*)",
        "Bash(helm install:*)",
        "Bash(helm upgrade:*)",
        "Bash(helm rollback:*)",
    ]
    _write_settings(repo, allow=["entry-a"], ask=infra_ask)

    with (
        patch("init_permissions.validator.resolve_profile", return_value=("work-app", None)),  # path says work-app
        patch("init_permissions.validator.run_check") as mock_check,
        patch(
            "init_permissions.validator.identify_applied_profile",
            return_value="work-infra",
        ),  # but actual ask matches work-infra
    ):
        from init_permissions.checker import CheckResult

        mock_check.return_value = CheckResult(repo_path=str(repo), profile="work-app", warnings=[])
        result = run_validate(repo)

    assert result.profile_mismatch is True
    assert result.detected_profile == "work-app"
    assert result.applied_profile == "work-infra"


# --- bulk_validate ---


def test_bulk_validate_returns_all_repos(tmp_path: Path) -> None:
    """bulk_validate() returns a ValidationResult for each discovered managed repo."""
    # Create two fake managed repos
    repo1 = tmp_path / "Developer" / "Work" / "repo1"
    repo2 = tmp_path / "Developer" / "Own" / "repo2"
    for r in (repo1, repo2):
        r.mkdir(parents=True)
        (r / ".claude").mkdir()

    with patch("init_permissions.validator._discover_managed_repos") as mock_discover:
        mock_discover.return_value = [repo1, repo2]
        with patch("init_permissions.validator.run_validate") as mock_validate:
            mock_validate.side_effect = [
                ValidationResult(
                    repo_path=str(repo1),
                    profile="work-app",
                    verdict=Verdict.PASS,
                    issues=[],
                    profile_mismatch=False,
                    detected_profile="work-app",
                    applied_profile="work-app",
                ),
                ValidationResult(
                    repo_path=str(repo2),
                    profile="own",
                    verdict=Verdict.PASS,
                    issues=[],
                    profile_mismatch=False,
                    detected_profile="own",
                    applied_profile="own",
                ),
            ]
            results = bulk_validate()

    assert len(results) == 2
    assert all(isinstance(r, ValidationResult) for r in results)


def test_bulk_validate_handles_mixed_results(tmp_path: Path) -> None:
    """bulk_validate() returns correct per-repo verdicts for mixed clean/drifted repos."""
    repo1 = tmp_path / "Developer" / "Work" / "clean-repo"
    repo2 = tmp_path / "Developer" / "Work" / "drifted-repo"
    for r in (repo1, repo2):
        r.mkdir(parents=True)
        (r / ".claude").mkdir()

    with patch("init_permissions.validator._discover_managed_repos") as mock_discover:
        mock_discover.return_value = [repo1, repo2]
        with patch("init_permissions.validator.run_validate") as mock_validate:
            mock_validate.side_effect = [
                ValidationResult(
                    repo_path=str(repo1),
                    profile="work-app",
                    verdict=Verdict.PASS,
                    issues=[],
                    profile_mismatch=False,
                    detected_profile="work-app",
                    applied_profile="work-app",
                ),
                ValidationResult(
                    repo_path=str(repo2),
                    profile="work-app",
                    verdict=Verdict.FAIL,
                    issues=["ask[] mismatch — extra: ['rogue'], missing: []"],
                    profile_mismatch=False,
                    detected_profile="work-app",
                    applied_profile="work-app",
                ),
            ]
            results = bulk_validate()

    verdicts = {Path(r.repo_path).name: r.verdict for r in results}
    assert verdicts["clean-repo"] == Verdict.PASS
    assert verdicts["drifted-repo"] == Verdict.FAIL


# --- Persistent profile override (repo-config.json `profile`) ---


def test_profile_override_suppresses_mismatch_and_passes(tmp_path: Path) -> None:
    """A repo-config.json profile override is the user's declared truth.

    When path detection says one profile but repo-config.json overrides to another,
    validator must NOT raise a profile-mismatch FAIL — the override is by definition
    the correct profile. The expected allow[]/ask[] are computed against the override,
    so a clean repo under override returns PASS.

    This is the noc-general-template scenario from 2026-04-07.
    """
    from init_permissions.models import RepoConfig

    repo = _make_managed_repo(tmp_path)
    # Path-detected profile would be work-infra (Developer/Work + assume tf nearby),
    # but the repo-config.json forces work-app.
    _write_settings(repo, allow=["entry-a"], ask=["ask-a"])
    expected = GeneratedSettings(allow=["entry-a"], ask=["ask-a"])

    overridden_repo_config = RepoConfig(profile="work-app")

    with (
        patch(
            "init_permissions.validator.resolve_profile",
            return_value=("work-app", overridden_repo_config),
        ),
        patch("init_permissions.validator.detect_profile", return_value="work-infra"),
        patch("init_permissions.validator.run_check") as mock_check,
        patch("init_permissions.validator.generate_settings", return_value=expected),
    ):
        from init_permissions.checker import CheckResult

        mock_check.return_value = CheckResult(repo_path=str(repo), profile="work-app", warnings=[])
        result = run_validate(repo)

    assert result.verdict == Verdict.PASS
    assert result.profile_mismatch is False
    assert result.profile == "work-app"
    assert result.detected_profile == "work-infra"
    assert result.applied_profile == "work-app"
