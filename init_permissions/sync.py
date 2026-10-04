"""Sync engine for regenerating settings.json across all managed repos.

Implements SYNC-01 (check mode dry-run preview), SYNC-02 (apply mode bulk regeneration),
and SYNC-03 (uniform dedup across all profiles).

The run_sync() function regenerates settings.json for a single repo, preserving any
top-level keys outside 'permissions' (e.g., hooks sections per D-03). bulk_sync()
discovers all managed repos and runs sync on each.

Safety model (post-2026-04-07 dotfiles incident):
- bulk_sync() snapshots the global allow list ONCE at the start and passes it
  to every per-repo generate_settings() call. This prevents mid-loop mutation
  when a repo's .claude/settings.json resolves (via a symlink) to the global
  source file.
- bulk_sync() filters out "self-write" repos whose settings.json would be
  written through to ~/.claude/settings.json. Running sync on such a repo is
  always self-destructive: the repo IS the global source, not a managed repo.
- detect_sync_hazards() surfaces these conditions as structured Hazards so
  dry-run callers can show them to the user before any file is written.
"""

from __future__ import annotations

import contextlib
import io
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from init_permissions.generator import (
    _GLOBAL_SETTINGS_PATH,
    generate_settings,
    load_global_allow,
    resolve_profile,
)
from init_permissions.layout import is_managed, settings_relpath
from init_permissions.validator import _discover_managed_repos

__all__ = [
    "BulkSyncPlan",
    "Hazard",
    "SyncAction",
    "SyncResult",
    "bulk_sync",
    "detect_sync_hazards",
    "run_sync",
]


@dataclass(frozen=True)
class Hazard:
    """A pre-apply safety issue detected by dry-run."""

    severity: Literal["BLOCK", "WARN", "INFO"]
    """BLOCK prevents apply; WARN surfaces to user but allows continuation."""

    category: str
    """Short identifier, e.g. 'self-write-collision', 'dangling-symlink'."""

    repo: Path | None
    """Affected repo, or None for global-scope hazards."""

    message: str
    """One-line user-facing summary."""

    explanation: str = ""
    """Optional multi-line detail explaining the impact and fix."""


def _realpath_or_none(path: Path) -> Path | None:
    """Return a resolved path, or None for non-existent / dangling entries."""
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def detect_sync_hazards(
    repos: list[Path],
    global_settings_path: Path = _GLOBAL_SETTINGS_PATH,
) -> list[Hazard]:
    """Inspect the planned sync set for pre-apply hazards.

    Currently, it detects two classes:
    - self-write-collision: a repo's settings.json resolves (via a symlink
      or bind mount) to the global settings file. Writing it would mutate
      the global source mid-loop, cascading incorrect generation to every
      subsequent repo.
    - dangling-symlink: a repo's settings.json exists as a symlink to a
      non-existent target. A write would follow the symlink into the void.

    Args:
        repos: The list of repo roots bulk_sync is about to process.
        global_settings_path: Path to the global ~/.claude/settings.json.

    Returns:
        List of Hazard records. Empty list means the plan is safe.
    """
    hazards: list[Hazard] = []
    global_real = _realpath_or_none(global_settings_path)

    for repo in repos:
        settings_path = repo / settings_relpath(repo)

        # Dangling symlink — is_symlink but the target doesn't exist.
        if settings_path.is_symlink() and not settings_path.exists():
            hazards.append(
                Hazard(
                    severity="WARN",
                    category="dangling-symlink",
                    repo=repo,
                    message=f"{settings_path} is a symlink to a non-existent target",
                    explanation="Writing this file would follow the symlink. Inspect the link target before syncing.",
                )
            )
            continue

        # Self-write collision — realpath of repo settings equals realpath of global.
        if global_real is not None and settings_path.exists():
            repo_real = _realpath_or_none(settings_path)
            if repo_real is not None and repo_real == global_real:
                hazards.append(
                    Hazard(
                        severity="BLOCK",
                        category="self-write-collision",
                        repo=repo,
                        message=(
                            f"{repo.name}/{settings_relpath(repo)} resolves to the "
                            "global settings source — writing it would mutate "
                            "the global allow list mid-loop"
                        ),
                        explanation=(
                            "This repo IS the global source (likely a dotfiles-style "
                            "checkout). It must NOT be processed by bulk_sync — "
                            "running sync on it wipes the global allow list and "
                            "cascades incorrect dedup to every subsequent repo. "
                            "Exclude it by removing .claude/ from the repo, or "
                            "avoid placing a symlink to the global file inside a managed path."
                        ),
                    )
                )

    return hazards


def _filter_self_writes(
    repos: list[Path],
    global_settings_path: Path = _GLOBAL_SETTINGS_PATH,
) -> tuple[list[Path], list[Path]]:
    """Split repos into (safe_to_sync, self_write_excluded).

    Self-write repos are those whose settings.json realpath matches the
    global settings file — running sync on them is self-destructive and
    must be skipped even when dry_run=False. They are returned in the
    second list so callers can log the exclusion.
    """
    global_real = _realpath_or_none(global_settings_path)
    if global_real is None:
        return list(repos), []

    safe: list[Path] = []
    excluded: list[Path] = []
    for repo in repos:
        settings_path = repo / settings_relpath(repo)
        if not settings_path.exists():
            safe.append(repo)
            continue
        repo_real = _realpath_or_none(settings_path)
        if repo_real == global_real:
            excluded.append(repo)
        else:
            safe.append(repo)
    return safe, excluded


class SyncAction(StrEnum):
    """The result of a sync operation for a single repo."""

    UNCHANGED = "UNCHANGED"
    """Settings already match generator output — no changes needed."""

    UPDATED = "UPDATED"
    """Settings were updated to match generator output."""

    CREATED = "CREATED"
    """Settings file was created from scratch (did not exist before)."""

    SKIPPED = "SKIPPED"
    """Path is not a managed repo — sync skipped."""

    WOULD_UPDATE = "WOULD_UPDATE"
    """Check mode: settings would be updated if sync ran without --check."""

    WOULD_CREATE = "WOULD_CREATE"
    """Check mode: settings file would be created if sync ran without --check."""

    ERROR = "ERROR"
    """An error occurred during sync for this repo."""


class SyncResult(BaseModel):
    """Result of a sync operation for a single repo path."""

    repo_path: str
    """Absolute path to the repository root."""

    action: SyncAction
    """What happened (or would happen in check mode) for this repo."""

    profile: str
    """Auto-detected profile name ('work-infra', 'work-app', 'own', or '' for skipped)."""

    entries_removed: int = 0
    """Number of allow[] entries in current settings that generator does not produce."""

    entries_added: int = 0
    """Number of allow[] entries the generator produces that are not in current settings."""

    error: str = ""
    """Error message if action is ERROR, empty otherwise."""


def run_sync(
    repo_path: Path,
    *,
    dry_run: bool = False,
    global_allow: set[str] | None = None,
) -> SyncResult:
    """Sync settings.json for a single repository against the current generator output.

    When applying (dry_run=False): regenerates settings.json, preserving all
    top-level keys outside 'permissions' (e.g., hooks sections per D-03).

    When previewing (dry_run=True): computes what would change without touching any files.

    Args:
        repo_path: Path to the repository root directory.
        dry_run: If True, preview changes without modifying any files.
        global_allow: Pre-loaded global allow snapshot from bulk_sync. When
            None (single-repo calls), generate_settings reads the global
            file from disk. When provided, every call uses the same
            snapshot — required for bulk operations to avoid mid-loop
            mutation of the shared global source file.

    Returns:
        SyncResult describing what happened (or would happen) for this repo.
    """
    path_str = str(repo_path)

    # Guard: skip non-managed paths
    if not is_managed(repo_path):
        return SyncResult(repo_path=path_str, action=SyncAction.SKIPPED, profile="")

    try:
        profile_name, repo_config = resolve_profile(repo_path)

        # Suppress stderr dedup notices from generate_settings (per Pitfall 6)
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            expected = generate_settings(
                profile_name,
                repo_path,
                repo_config,
                global_allow=global_allow,
            )

        settings_path = repo_path / settings_relpath(repo_path)

        # Case 1: settings.json does not exist
        if not settings_path.exists():
            if dry_run:
                return SyncResult(
                    repo_path=path_str,
                    action=SyncAction.WOULD_CREATE,
                    profile=profile_name,
                )
            # Apply mode: write the file
            expected_dict = expected.to_settings_dict()
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(expected_dict, indent=2) + "\n", encoding="utf-8")
            return SyncResult(
                repo_path=path_str,
                action=SyncAction.CREATED,
                profile=profile_name,
            )

        # Case 2: settings.json exists — compare permissions only
        current_data = json.loads(settings_path.read_text(encoding="utf-8"))
        current_permissions = current_data.get("permissions", {})
        current_allow = set(current_permissions.get("allow", []))
        current_ask = set(current_permissions.get("ask", []))

        expected_allow = set(expected.allow)
        expected_ask = set(expected.ask)

        permissions_match = sorted(current_allow) == sorted(expected_allow) and sorted(current_ask) == sorted(expected_ask)

        if permissions_match:
            return SyncResult(
                repo_path=path_str,
                action=SyncAction.UNCHANGED,
                profile=profile_name,
            )

        # Permissions differ — compute diff counts
        entries_removed = len(current_allow - expected_allow)
        entries_added = len(expected_allow - current_allow)

        if dry_run:
            return SyncResult(
                repo_path=path_str,
                action=SyncAction.WOULD_UPDATE,
                profile=profile_name,
                entries_removed=entries_removed,
                entries_added=entries_added,
            )

        # Apply mode: replace only the permissions section, preserving all other top-level keys (D-03)
        new_permissions = expected.to_settings_dict().get("permissions", {})
        current_data["$schema"] = expected.schema_url
        current_data["permissions"] = new_permissions
        settings_path.write_text(json.dumps(current_data, indent=2) + "\n", encoding="utf-8")

        return SyncResult(
            repo_path=path_str,
            action=SyncAction.UPDATED,
            profile=profile_name,
            entries_removed=entries_removed,
            entries_added=entries_added,
        )

    except (OSError, ValueError) as exc:
        return SyncResult(
            repo_path=path_str,
            action=SyncAction.ERROR,
            profile="",
            error=str(exc),
        )


@dataclass(frozen=True)
class BulkSyncPlan:
    """Result of bulk_sync: per-repo results plus pre-apply diagnostics.

    Callers (CLI) display `hazards` and `excluded` as a safety section
    before the per-repo result table. `hazards` is empty when the plan is
    clean; `excluded` lists repos silently dropped because their
    settings.json resolves to the global source file.
    """

    results: list[SyncResult]
    hazards: list[Hazard]
    excluded: list[Path]


def bulk_sync(*, dry_run: bool = False) -> BulkSyncPlan:
    """Sync settings.json for all managed repos under the own/work prefixes from config.toml.

    Discovers repos by scanning for directories containing .claude/ under each
    managed prefix, then:

    1. Snapshots the global allow list ONCE up front and reuses it for every
       per-repo generate_settings() call (prevents mid-loop drift when a
       write from one repo mutates the shared global source).
    2. Detects pre-apply hazards (self-write collisions, dangling symlinks)
       so the caller can surface them before anything is written.
    3. Excludes self-write-collision repos from the sync set — running sync
       on them is always self-destructive.
    4. Runs the remaining repos through run_sync() with the snapshot.

    Args:
        dry_run: If True, preview all changes without modifying any files.

    Returns:
        BulkSyncPlan with results, hazards, and the excluded repo list.
    """
    # Resolve module-level global path at call time so test patches land.
    global_path = _GLOBAL_SETTINGS_PATH

    repos = _discover_managed_repos()
    hazards = detect_sync_hazards(repos, global_settings_path=global_path)
    safe, excluded = _filter_self_writes(repos, global_settings_path=global_path)

    # Snapshot the global allow list ONCE. Every per-repo generate_settings()
    # call in this loop must see the same value — otherwise a repo whose
    # settings.json writes back to the global source would cascade incorrect
    # dedup to every subsequent repo. Self-write repos are already excluded
    # above, but the snapshot is a low-cost second line of defense.
    global_allow_snapshot = load_global_allow(global_path)

    results = [run_sync(repo, dry_run=dry_run, global_allow=global_allow_snapshot) for repo in safe]
    results.sort(key=lambda r: r.repo_path)
    return BulkSyncPlan(results=results, hazards=hazards, excluded=sorted(excluded))
