"""Settings.json generator: merges profile base + repo-config into settings dict.

Generation flow (per D-12, D-14, Pitfall 1):
1. Load profile by name via get_profile()
2. Merge stack tools for work-app profile
3. Merge optional repo-config.json entries
4. Deduplicate against global ~/.claude/settings.json allow[]
5. Return GeneratedSettings ready for to_settings_dict()
"""

import json
from pathlib import Path

from init_permissions.detector import detect_profile, detect_stacks
from init_permissions.models import GeneratedSettings, RepoConfig
from init_permissions.profiles import STACK_TOOLS, get_profile

_REPO_CONFIG_PATH = ".claude/repo-config.json"
_GLOBAL_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def resolve_profile(repo_path: Path) -> tuple[str, RepoConfig | None]:
    """Resolve the effective profile for a repo, honoring repo-config override.

    Loads .claude/repo-config.json once. If it sets `profile`, that takes
    precedence over path-based detection. Returns the loaded RepoConfig so
    callers don't have to re-load it.

    Args:
        repo_path: Path to the repository root directory.

    Returns:
        Tuple of (effective profile name, optional RepoConfig).
    """
    repo_config = load_repo_config(repo_path)
    if repo_config is not None and repo_config.profile is not None:
        return repo_config.profile, repo_config
    return detect_profile(repo_path), repo_config


def _merge_repo_config(allow: list[str], repo_config: RepoConfig) -> None:
    """Merge repo-config.json entries into the allow list, skipping duplicates."""
    webfetch_entries = [f"WebFetch(domain:{d})" for d in repo_config.webfetch_domains]
    for entry in [*webfetch_entries, *repo_config.mcp_tools, *repo_config.bash_commands, *repo_config.skills]:
        if entry not in allow:
            allow.append(entry)


def _merge_stack_tools(allow: list[str], repo_path: Path) -> None:
    """Detect repo stacks and merge their build/test tools into allow[]."""
    stacks = detect_stacks(repo_path)
    for stack in stacks:
        if toolset := STACK_TOOLS.get(stack):
            for entry in toolset.allow:
                if entry not in allow:
                    allow.append(entry)


def generate_settings(
    profile_name: str,
    repo_path: Path,
    repo_config: RepoConfig | None = None,
    global_settings_path: Path = _GLOBAL_SETTINGS_PATH,
    global_allow: set[str] | None = None,
) -> GeneratedSettings:
    """Generate permission settings for a repository.

    Loads the named profile, merges stack-specific tools (for work-app),
    merges repo-config overrides, and deduplicates against the global
    allow[] to avoid noisy per-project redundancy.

    Args:
        profile_name: One of 'work-infra', 'work-app', or 'own'.
        repo_path: Path to the repository root directory.
        repo_config: Optional per-repo customization. If None, skips merge.
        global_settings_path: Path to global ~/.claude/settings.json.
        global_allow: Pre-loaded global allow snapshot. When provided,
            skips reading from disk — caller is responsible for passing a
            consistent value across a bulk operation. Bulk callers MUST
            pass this to avoid race conditions when one repo write
            mutates the global source mid-loop.

    Returns:
        GeneratedSettings with deduplicated allow[] and ask[] arrays.
    """
    profile = get_profile(profile_name)

    # Start with copies to avoid mutating the profile singletons
    allow: list[str] = list(profile.allow)
    ask: list[str] = list(profile.ask)

    # For work-app: merge stack-detected build tools into allow[]
    if profile_name == "work-app":
        _merge_stack_tools(allow, repo_path)

    # Merge repo-config.json entries (per D-12, D-14)
    if repo_config is not None:
        _merge_repo_config(allow, repo_config)

    # Deduplicate against global allow[] (per Pitfall 1).
    # Use caller-provided snapshot if present; otherwise read from disk.
    effective_global_allow: set[str] = load_global_allow(global_settings_path) if global_allow is None else global_allow
    duplicates_removed = [entry for entry in allow if entry in effective_global_allow]
    allow = [entry for entry in allow if entry not in effective_global_allow]

    if duplicates_removed:
        import sys

        print(
            f"[init-permissions] Removed {len(duplicates_removed)} entries already in global settings: {duplicates_removed}",
            file=sys.stderr,
        )

    return GeneratedSettings(allow=allow, ask=ask)


def load_repo_config(repo_path: Path) -> RepoConfig | None:
    """Load per-repo customization from .claude/repo-config.json.

    Reads and validates the repo-config.json file if it exists.
    Returns None silently if the file does not exist.

    Args:
        repo_path: Path to the repository root directory.

    Returns:
        Validated RepoConfig instance, or None if file not found.

    Raises:
        ValueError: If the file exists but fails Pydantic validation.
        json.JSONDecodeError: If the file exists but is not valid JSON.
    """
    config_path = repo_path / _REPO_CONFIG_PATH
    if not config_path.exists():
        return None
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    return RepoConfig.model_validate(raw)


def write_settings(settings: GeneratedSettings, repo_path: Path) -> Path:
    """Write generated settings to .claude/settings.json in the repo.

    Creates the .claude/ directory if it does not exist.
    Writes with 2-space indent and a trailing newline.

    Args:
        settings: The settings object to serialize.
        repo_path: Path to the repository root directory.

    Returns:
        Absolute path to the written settings.json file.
    """
    claude_dir = repo_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    output_path = claude_dir / "settings.json"
    content = json.dumps(settings.to_settings_dict(), indent=2) + "\n"
    output_path.write_text(content, encoding="utf-8")
    return output_path.resolve()


def load_global_allow(global_settings_path: Path = _GLOBAL_SETTINGS_PATH) -> set[str]:
    """Read the global allow[] list from ~/.claude/settings.json.

    Returns an empty set if the file does not exist or has no permissions.

    Args:
        global_settings_path: Path to the global settings.json file.

    Returns:
        Set of permission entry strings in the global allow[].
    """
    if not global_settings_path.exists():
        return set()
    raw = json.loads(global_settings_path.read_text(encoding="utf-8"))
    permissions = raw.get("permissions", {})
    return set(permissions.get("allow", []))
