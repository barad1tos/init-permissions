"""Layer 3: Rule-based assertions for permission profile safety invariants.

These tests enforce D-13, D-18, D-19, D-22 at the data layer:
- No terraform mutate ops in allow[]
- git push correctly placed in ask[] for work repos, absent for own
- No globally-denied commands (curl) leak into allow[]
- All three profiles are retrievable by name
"""

from pathlib import Path

import pytest

from init_permissions.profiles import OWN, PROFILES, WORK_APP, WORK_INFRA, get_profile

_GLOBAL_BASELINE_ALLOW = [
    "Bash(./gradlew:*)",
    "Bash(cargo build:*)",
    "Bash(cargo clippy:*)",
    "Bash(cargo run:*)",
    "Bash(cargo test:*)",
    "Bash(docker build:*)",
    "Bash(docker images:*)",
    "Bash(docker logs:*)",
    "Bash(docker ps:*)",
    "Bash(docker push:*)",
    "Bash(go build:*)",
    "Bash(go mod:*)",
    "Bash(go run:*)",
    "Bash(go test:*)",
    "Bash(gradle:*)",
    "Bash(make:*)",
    "Bash(npm:*)",
    "Bash(pnpm:*)",
    "Bash(pytest:*)",
    "Bash(yarn:*)",
    "Skill(claude-skill-auditor)",
    "Skill(lessons-learned)",
    "WebFetch(domain:apilist.tronscan.org)",
    "WebFetch(domain:gallery.ecr.aws)",
    "WebFetch(domain:pfisterer.dev)",
    "WebFetch(domain:repost.aws)",
    "WebFetch(domain:tronscan.org)",
    "WebFetch(domain:www.solutiontoolkit.com)",
]


def test_work_infra_no_terraform_apply_in_allow() -> None:
    """D-18: terraform apply must NOT be in allow — requires explicit confirmation."""
    assert "Bash(terraform apply:*)" not in WORK_INFRA.allow


def test_work_infra_terraform_apply_in_ask() -> None:
    """D-18: terraform apply MUST be in ask for work-infra repos."""
    assert "Bash(terraform apply:*)" in WORK_INFRA.ask


def test_work_infra_terraform_import_in_ask() -> None:
    """D-18: terraform import is a mutate op — must be in ask, not allow."""
    assert "Bash(terraform import:*)" in WORK_INFRA.ask


def test_work_infra_git_push_in_ask() -> None:
    """Work-infra downgrades git push from global allow to explicit confirmation."""
    assert "Bash(git push:*)" in WORK_INFRA.ask


def test_work_infra_git_push_not_in_allow() -> None:
    """git push must not be in both allow and ask simultaneously for work-infra."""
    assert "Bash(git push:*)" not in WORK_INFRA.allow


def test_work_app_git_push_in_ask() -> None:
    """Work-app downgrades git push from global allow to explicit confirmation."""
    assert "Bash(git push:*)" in WORK_APP.ask


def test_work_app_git_push_not_in_allow() -> None:
    """git push must not be in both allow and ask simultaneously for work-app."""
    assert "Bash(git push:*)" not in WORK_APP.allow


def test_work_app_has_npm_publish_in_ask() -> None:
    """npm publish is a publishing op — must require confirmation for work-app repos."""
    assert "Bash(npm publish:*)" in WORK_APP.ask


def test_own_no_git_push_in_ask() -> None:
    """D-22: own profile has empty ask[] — git push stays auto-approved."""
    assert "Bash(git push:*)" not in OWN.ask


def test_own_ask_is_empty() -> None:
    """D-22: own profile has fully empty ask[] (no restrictions beyond global deny)."""
    assert OWN.ask == []


def test_no_profile_has_curl() -> None:
    """D-19: curl is globally denied — must not appear in any profile's allow[]."""
    for profile in PROFILES.values():
        for entry in profile.allow:
            assert not entry.startswith("Bash(curl"), f"Profile '{profile.name}' has curl entry in allow[]: {entry}"


def test_no_profile_has_deny_field() -> None:
    """D-13: profiles have no deny field — global deny is the unbreakable floor."""
    for profile in PROFILES.values():
        assert not hasattr(profile, "deny"), f"Profile '{profile.name}' unexpectedly has a deny field"


def test_all_profiles_exist() -> None:
    """All three profile names are retrievable via get_profile()."""
    for name in ("work-infra", "work-app", "own"):
        profile = get_profile(name)
        assert profile.name == name


def test_get_profile_raises_on_unknown() -> None:
    """get_profile raises KeyError for unrecognized profile names."""
    with pytest.raises(KeyError, match="Unknown profile"):
        get_profile("nonexistent")


# --- GLOBAL profile tests ---------------------------------------------------


def test_global_profile_exists() -> None:
    """get_profile('global') returns a profile named 'global'."""
    import typing

    from init_permissions.models import RepoConfig
    from init_permissions.profiles import GLOBAL

    profile = get_profile("global")
    assert profile.name == "global"
    assert profile is GLOBAL
    # Keep imports referenced
    assert RepoConfig is not None
    assert typing is not None


def test_global_has_read_claude_dir_entry() -> None:
    """GLOBAL allow grants reading the global config tree by absolute //path, without a baked-in user."""
    from init_permissions.profiles import GLOBAL, GLOBAL_CONFIG_READ

    assert f"Read(/{Path.home()}/.claude/**)" == GLOBAL_CONFIG_READ
    assert GLOBAL_CONFIG_READ in GLOBAL.allow


def test_global_ask_is_empty() -> None:
    """GLOBAL profile has empty ask[]."""
    from init_permissions.profiles import GLOBAL

    assert GLOBAL.ask == []


def test_global_baseline_preserved() -> None:
    """Every baseline entry from the shipped ~/.claude/settings.json is present in GLOBAL."""
    from init_permissions.profiles import GLOBAL

    for entry in _GLOBAL_BASELINE_ALLOW:
        assert entry in GLOBAL.allow, f"Baseline entry missing from GLOBAL: {entry}"


def test_npx_is_granted_per_project_only() -> None:
    """npx stays out of the global layer; own repos and node work repos still get it."""
    from init_permissions.profiles import GLOBAL, NPX, STACK_TOOLS

    assert NPX not in GLOBAL.allow
    assert NPX in OWN.allow
    assert NPX in STACK_TOOLS["node"].allow


def test_global_not_in_repo_config_literal() -> None:
    """RepoConfig.profile Literal must not contain 'global' — global is CLI-only."""
    import typing

    from init_permissions.models import RepoConfig

    annotation = RepoConfig.model_fields["profile"].annotation
    args = typing.get_args(annotation)
    # Flatten any nested Literal args
    literal_values: list[str] = []
    for arg in args:
        if arg is type(None):
            continue
        literal_values.extend(typing.get_args(arg) or [arg])
    assert "global" not in literal_values
