"""Permission validator for managed repos.

Implements VALID-01 (single-repo PASS/WARN/FAIL audit), VALID-02 (bulk validation
of all managed repos), and VALID-03 (profile mismatch detection).

The run_validate() function builds on run_check() from Phase 13, adding Verdict
classification and profile mismatch detection. bulk_validate() discovers all
managed repos and returns a sorted list of ValidationResult.
"""

from __future__ import annotations

import contextlib
import io
import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from init_permissions.checker import run_check
from init_permissions.detector import detect_profile
from init_permissions.generator import generate_settings, resolve_profile
from init_permissions.layout import is_managed, load_layout
from init_permissions.profiles import PROFILES

_CLAUDE_DIR = ".claude"


class Verdict(StrEnum):
    """Validation verdict for a single repo."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class ValidationResult(BaseModel):
    """Full validation result for a single repo path."""

    repo_path: str
    """Absolute path to the repository root."""

    profile: str
    """Auto-detected profile name ('work-infra', 'work-app', 'own', or '' for non-managed)."""

    verdict: Verdict
    """Overall validation verdict: PASS, WARN, or FAIL."""

    issues: list[str] = []
    """Specific problems found during validation."""

    profile_mismatch: bool = False
    """True when the actual ask[] signature doesn't match the auto-detected profile (VALID-03)."""

    detected_profile: str = ""
    """Profile auto-detected from repo path."""

    applied_profile: str = ""
    """Profile inferred from actual ask[] signature — may differ from detected_profile."""


def identify_applied_profile(actual_ask: set[str]) -> str:
    """Identify which profile was applied based on the actual ask[] entries.

    Matches by checking actual set membership against each known profile
    definition. Returns 'unknown' if no profile matches.

    Args:
        actual_ask: The actual set of ask[] entries from settings.json.

    Returns:
        Profile name ('work-infra', 'work-app', 'own') or 'unknown' if no match.
    """
    for profile_name, profile in PROFILES.items():
        expected_ask = set(profile.ask)
        if actual_ask == expected_ask:
            return profile_name
    return "unknown"


def _classify_warnings(warnings: list[str]) -> tuple[list[str], Verdict]:
    """Classify check warnings into issues and a verdict.

    MISSING and DRIFT warnings produce FAIL; actionable hints (FIX:, CHECK:)
    are filtered out.

    Returns:
        Tuple of (an issues list, verdict).
    """
    issues: list[str] = []
    verdict = Verdict.PASS
    for warning in warnings:
        if warning.startswith(("FIX:", "CHECK:")):
            continue
        if warning.startswith(("MISSING:", "DRIFT:")):
            issues.append(warning)
            verdict = Verdict.FAIL
    return issues, verdict


def _detect_profile_mismatch(settings_path: Path, detected: str) -> tuple[str, bool, list[str]]:
    """Detect whether the applied profile differs from the auto-detected one.

    Args:
        settings_path: Path to the repo's settings.json file.
        detected: The auto-detected profile name from the repo path.

    Returns:
        Tuple of (applied_profile, mismatch_flag, extra_issues).
    """
    if not settings_path.exists():
        return detected, False, []

    try:
        actual_data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return detected, False, []

    actual_ask = set(actual_data.get("permissions", {}).get("ask", []))
    applied = identify_applied_profile(actual_ask)

    if applied == detected:
        return applied, False, []

    issue = f"Profile mismatch: path detection says '{detected}' but ask[] signature matches '{applied}'"
    return applied, True, [issue]


def run_validate(repo_path: Path) -> ValidationResult:
    """Validate a repository's permissions against its expected profile.

    Returns PASS for non-managed paths (nothing to validate). For managed repos:
    - FAIL: missing settings.json, missing expected allow[] entries, or ask[] drift
    - WARN: extra allow[] entries beyond the profile (harmless customization per D-09)
    - PASS: settings match the expected profile exactly

    Also detects profile mismatch (VALID-03): when the actual ask[] signature
    (via identify_applied_profile) doesn't match the auto-detected profile.

    Args:
        repo_path: Path to the repository root directory to validate.

    Returns:
        ValidationResult with verdict, issues, and profile mismatch flag.
    """
    path_str = str(repo_path)

    # Guard: non-managed paths get an immediate PASS — nothing to validate
    if not is_managed(repo_path):
        return ValidationResult(
            repo_path=path_str,
            profile="",
            verdict=Verdict.PASS,
        )

    # Use run_check() for structural comparison (reuses Phase 13 logic)
    check_result = run_check(repo_path)

    # Resolve effective profile (honors .claude/repo-config.json `profile` override)
    effective, repo_config = resolve_profile(repo_path)
    detected = detect_profile(repo_path)
    has_override = repo_config is not None and repo_config.profile is not None

    issues, verdict = _classify_warnings(check_result.warnings)

    # Profile mismatch detection (VALID-03) — skipped when an explicit override is set,
    # because the override IS the user's declared truth, nothing to mismatch against.
    settings_path = repo_path / _CLAUDE_DIR / "settings.json"
    if has_override:
        applied_profile, profile_mismatch, mismatch_issues = effective, False, []
    else:
        applied_profile, profile_mismatch, mismatch_issues = _detect_profile_mismatch(settings_path, effective)
    issues.extend(mismatch_issues)
    if profile_mismatch and verdict == Verdict.PASS:
        verdict = Verdict.FAIL

    # Extra allow[] entries: WARN if no other failures
    if settings_path.exists() and verdict == Verdict.PASS:
        try:
            actual_data = json.loads(settings_path.read_text(encoding="utf-8"))
            actual_allow = set(actual_data.get("permissions", {}).get("allow", []))
            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                expected = generate_settings(effective, repo_path, repo_config)
            expected_allow = set(expected.allow)
            extra_allow = actual_allow - expected_allow
            if extra_allow:
                issues.append(f"allow[] has {len(extra_allow)} extra entries beyond profile (customization, harmless): {sorted(extra_allow)}")
                verdict = Verdict.WARN
        except (json.JSONDecodeError, OSError):
            pass

    return ValidationResult(
        repo_path=path_str,
        profile=effective,
        verdict=verdict,
        issues=issues,
        profile_mismatch=profile_mismatch,
        detected_profile=detected,
        applied_profile=applied_profile,
    )


def _scan_directory_for_repos(directory: Path) -> list[Path]:
    """Scan a directory up to two levels deep for repos containing .claude/.

    Level 1: direct children (e.g., github.com/me/ -> dotfiles/).
    Level 2: grandchildren (e.g., github.com/ -> me/dotfiles/).
    """
    repos: list[Path] = []
    for child in directory.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        if (child / _CLAUDE_DIR).is_dir():
            repos.append(child)
            continue
        for grandchild in child.iterdir():
            if grandchild.is_dir() and (grandchild / _CLAUDE_DIR).is_dir():
                repos.append(grandchild)
    return repos


def _discover_managed_repos() -> list[Path]:
    """Discover all managed repos under the own/work prefixes from config.toml.

    Returns:
        Sorted list of repo root Paths; empty when no config exists.
    """
    layout = load_layout()
    if layout is None:
        return []
    repos: set[Path] = set()
    for root in layout.managed_roots():
        if root.is_dir():
            repos.update(_scan_directory_for_repos(root))
    return sorted(repos)


def bulk_validate() -> list[ValidationResult]:
    """Validate all managed repos under the own/work prefixes from config.toml.

    Discovers repos by scanning for directories containing .claude/ under
    each managed prefix. Calls run_validate() for each discovered repo.

    Returns:
        List of ValidationResult sorted by repo_path.
    """
    repos = _discover_managed_repos()
    results = [run_validate(repo) for repo in repos]
    return sorted(results, key=lambda r: r.repo_path)
