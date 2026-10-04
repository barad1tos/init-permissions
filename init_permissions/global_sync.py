"""Sync engine for ~/.claude/settings.json.

Manages the global profile in isolation — does NOT use load_global_allow
(this file IS the global source) and does NOT touch repo paths. Atomic
writes preserve every top-level key outside `permissions` and `$schema`
(notably `hooks` and `enabledPlugins`).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from init_permissions.models import GeneratedSettings
from init_permissions.profiles import get_profile
from init_permissions.sync import SyncAction, SyncResult
from init_permissions.validator import ValidationResult, Verdict

_DEFAULT_GLOBAL_PATH = Path.home() / ".claude" / "settings.json"

__all__ = ["run_global_check", "run_global_sync", "run_global_validate"]


def _build_expected() -> GeneratedSettings:
    """Build the expected GeneratedSettings for the GLOBAL profile."""
    profile = get_profile("global")
    return GeneratedSettings(allow=list(profile.allow), ask=list(profile.ask))


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically: temp file in target dir, then os.replace()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".global-sync-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def run_global_sync(
    global_path: Path = _DEFAULT_GLOBAL_PATH,
    *,
    dry_run: bool = False,
) -> SyncResult:
    """Sync ~/.claude/settings.json against the GLOBAL profile.

    Preserves every top-level key outside `permissions` and `$schema`.
    Returns a SyncResult describing the action taken (or that would be taken).
    """
    path_str = str(global_path)
    try:
        expected = _build_expected()
        expected_dict = expected.to_settings_dict()
        expected_permissions = expected_dict["permissions"]
        expected_allow = set(expected.allow)
        expected_ask = set(expected.ask)

        # Case 1: file does not exist
        if not global_path.exists():
            if dry_run:
                return SyncResult(
                    repo_path=path_str,
                    action=SyncAction.WOULD_CREATE,
                    profile="global",
                    entries_added=len(expected_allow),
                )
            _atomic_write_json(global_path, expected_dict)
            return SyncResult(
                repo_path=path_str,
                action=SyncAction.CREATED,
                profile="global",
                entries_added=len(expected_allow),
            )

        # Case 2: file exists — compare permissions only
        current_data = json.loads(global_path.read_text(encoding="utf-8"))
        current_permissions = current_data.get("permissions", {})
        current_allow = set(current_permissions.get("allow", []))
        current_ask = set(current_permissions.get("ask", []))

        if current_allow == expected_allow and current_ask == expected_ask:
            return SyncResult(
                repo_path=path_str,
                action=SyncAction.UNCHANGED,
                profile="global",
            )

        entries_removed = len(current_allow - expected_allow)
        entries_added = len(expected_allow - current_allow)

        if dry_run:
            return SyncResult(
                repo_path=path_str,
                action=SyncAction.WOULD_UPDATE,
                profile="global",
                entries_removed=entries_removed,
                entries_added=entries_added,
            )

        current_data["$schema"] = expected.schema_url
        current_data["permissions"] = expected_permissions
        _atomic_write_json(global_path, current_data)
        return SyncResult(
            repo_path=path_str,
            action=SyncAction.UPDATED,
            profile="global",
            entries_removed=entries_removed,
            entries_added=entries_added,
        )
    except (OSError, ValueError) as exc:
        return SyncResult(
            repo_path=path_str,
            action=SyncAction.ERROR,
            profile="global",
            error=str(exc),
        )


def run_global_check(global_path: Path = _DEFAULT_GLOBAL_PATH) -> list[str]:
    """Return drift warning lines for ~/.claude/settings.json. Empty = clean."""
    if not global_path.exists():
        return [f"MISSING: {global_path}"]
    try:
        current_data = json.loads(global_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"DRIFT: global settings unreadable: {exc}"]

    current_permissions = current_data.get("permissions", {})
    current_allow = set(current_permissions.get("allow", []))
    current_ask = set(current_permissions.get("ask", []))

    expected = _build_expected()
    expected_allow = set(expected.allow)
    expected_ask = set(expected.ask)

    warnings: list[str] = []
    missing_allow = expected_allow - current_allow
    if missing_allow:
        warnings.append(f"DRIFT: global allow missing {len(missing_allow)} entries: {sorted(missing_allow)}")
    if current_ask != expected_ask:
        warnings.append(f"DRIFT: global ask drift: expected={sorted(expected_ask)}, actual={sorted(current_ask)}")
    return warnings


def run_global_validate(global_path: Path = _DEFAULT_GLOBAL_PATH) -> ValidationResult:
    """Validate ~/.claude/settings.json against the GLOBAL profile.

    PASS — allow superset matches & ask exact-set matches.
    WARN — extra allow entries beyond GLOBAL (harmless customization).
    FAIL — missing allow entries OR any ask drift OR file missing/unreadable.
    """
    path_str = str(global_path)
    if not global_path.exists():
        return ValidationResult(
            repo_path=path_str,
            profile="global",
            verdict=Verdict.FAIL,
            issues=[f"MISSING: {global_path}"],
        )
    try:
        current_data = json.loads(global_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return ValidationResult(
            repo_path=path_str,
            profile="global",
            verdict=Verdict.FAIL,
            issues=[f"unreadable: {exc}"],
        )

    current_permissions = current_data.get("permissions", {})
    current_allow = set(current_permissions.get("allow", []))
    current_ask = set(current_permissions.get("ask", []))

    expected = _build_expected()
    expected_allow = set(expected.allow)
    expected_ask = set(expected.ask)

    issues: list[str] = []
    verdict = Verdict.PASS

    missing_allow = expected_allow - current_allow
    if missing_allow:
        issues.append(f"allow[] missing {len(missing_allow)} required entries: {sorted(missing_allow)}")
        verdict = Verdict.FAIL

    if current_ask != expected_ask:
        issues.append(f"ask[] drift: expected={sorted(expected_ask)}, actual={sorted(current_ask)}")
        verdict = Verdict.FAIL

    if verdict == Verdict.PASS:
        extra_allow = current_allow - expected_allow
        if extra_allow:
            issues.append(f"allow[] has {len(extra_allow)} extra entries beyond GLOBAL (harmless): {sorted(extra_allow)}")
            verdict = Verdict.WARN

    return ValidationResult(
        repo_path=path_str,
        profile="global",
        verdict=verdict,
        issues=issues,
    )
