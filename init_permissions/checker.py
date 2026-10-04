"""Permission health checker for managed repos.

Implements DET-01 (missing settings.json warning), DET-02 (new repo detection),
and DET-03 (drift detection against expected profile).

The run_check() function is the Python-side logic called by the SessionStart hook
(Plan 02). It examines a repo path and returns structured warnings.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from pydantic import BaseModel

from init_permissions.generator import generate_settings, resolve_profile
from init_permissions.layout import is_managed, settings_relpath


class CheckResult(BaseModel):
    """Result of a permission health check for a single repo path."""

    repo_path: str
    profile: str = ""
    warnings: list[str] = []


def run_check(repo_path: Path) -> CheckResult:
    """Check permission health for a repository path.

    Returns a CheckResult with an empty warnings list for:
    - Non-managed paths (outside the own/work prefixes in config.toml)
    - Clean repos whose settings.json matches the expected profile

    Returns a CheckResult with warnings for:
    - DET-01/02: Managed repo missing .claude/settings.json
    - DET-03: settings.json whose allow[] or ask[] has drifted from profile

    Args:
        repo_path: Path to the repository root directory to check.

    Returns:
        CheckResult with repo_path, detected profile name, and collected warnings.
    """
    path_str = str(repo_path)

    # Guard clause: skip non-managed paths (D-04)
    if not is_managed(repo_path):
        return CheckResult(repo_path=path_str)

    settings_relative = settings_relpath(repo_path)
    settings_path = repo_path / settings_relative

    # Resolve effective profile once (honors .claude/repo-config.json `profile` override)
    profile_name, repo_config = resolve_profile(repo_path)

    # DET-01 / DET-02: Missing settings.json (covers both "missing .claude/" and
    # ".claude/ exists but settings.json absent" — same code path, different message)
    if not settings_path.exists():
        return CheckResult(
            repo_path=path_str,
            profile=profile_name,
            warnings=[
                f"MISSING: No {settings_relative} for {repo_path.name} (detected profile: {profile_name})",
                f"FIX: perms-gen  # or: perms generate {repo_path}",
                f"CHECK: perms-check  # or: perms validate {repo_path}",
            ],
        )

    # DET-03: Drift detection — compare only permissions.allow[] and permissions.ask[]

    # Suppress stderr dedup notices from generate_settings (per Pitfall 6)
    stderr_buf = io.StringIO()
    with contextlib.redirect_stderr(stderr_buf):
        expected = generate_settings(profile_name, repo_path, repo_config)

    actual = json.loads(settings_path.read_text(encoding="utf-8"))
    actual_permissions = actual.get("permissions", {})
    actual_allow = set(actual_permissions.get("allow", []))
    actual_ask = set(actual_permissions.get("ask", []))

    expected_allow = set(expected.allow)
    expected_ask = set(expected.ask)

    warnings: list[str] = []

    # allow[] must be a superset of expected (D-06): extra entries in actual are harmless
    missing_allow = expected_allow - actual_allow
    if missing_allow:
        warnings.append(f"DRIFT: allow[] missing {len(missing_allow)} expected entries: {sorted(missing_allow)}")

    # ask[] must be an exact set match (security-critical per D-06)
    extra_ask = actual_ask - expected_ask
    missing_ask = expected_ask - actual_ask
    if extra_ask or missing_ask:
        parts = []
        if extra_ask:
            parts.append(f"extra: {sorted(extra_ask)}")
        if missing_ask:
            parts.append(f"missing: {sorted(missing_ask)}")
        warnings.append(f"DRIFT: ask[] mismatch — {', '.join(parts)}")

    # Append actionable fix commands for each type of drift found
    if warnings:
        warnings.extend(
            (
                "FIX: perms-diff  # preview changes  (or: perms sync --check)",
                f"FIX: perms-gen  # regenerate this repo  (or: perms generate {repo_path})",
                f"CHECK: perms-check  # verify after fix  (or: perms validate {repo_path})",
            )
        )
    return CheckResult(repo_path=path_str, profile=profile_name, warnings=warnings)
