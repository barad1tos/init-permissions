"""Permission profile definitions for Work-Infra, Work-App, and Own repo categories.

Per design decisions:
- D-13: No deny entries in profiles — global deny is the unbreakable floor.
- D-18: No terraform mutate ops in allow[]. Mutate stays in ask[].
- D-22: Own profile has empty ask[] — git push stays auto-approved.
- D-19: Use WebFetch(domain:X) instead of Bash(curl *) — curl is globally denied.

Note: GLOBAL is reachable only via `init-permissions global ...` — never returned
by detect_profile(). It manages ~/.claude/settings.json itself.
"""

from pathlib import Path

from init_permissions.models import PermissionProfile, StackToolset

# Command groups shared by several profiles
MAKE = "Bash(make:*)"
DOCKER_READ = ["Bash(docker ps:*)", "Bash(docker images:*)", "Bash(docker logs:*)"]
DOCKER_WRITE = ["Bash(docker build:*)", "Bash(docker push:*)"]
GIT_PUSH = "Bash(git push:*)"
# npx runs arbitrary registry packages, so only per-project profiles grant it
NPX = "Bash(npx:*)"
# Claude Code reads //<absolute path>; built at runtime so no username is baked in
GLOBAL_CONFIG_READ = f"Read(/{Path.home()}/.claude/**)"

# Stack detection: maps stack identifier to file markers and allowed commands.
# Used by Work-App profile to add only relevant build tools per detected stack.
STACK_TOOLS: dict[str, StackToolset] = {
    "node": StackToolset(
        markers=["package.json"],
        allow=["Bash(npm:*)", NPX, "Bash(yarn:*)", "Bash(pnpm:*)"],
    ),
    "python": StackToolset(
        markers=["pyproject.toml", "setup.py", "requirements.txt"],
        allow=["Bash(pytest:*)"],
    ),
    "go": StackToolset(
        markers=["go.mod"],
        allow=[
            "Bash(go build:*)",
            "Bash(go test:*)",
            "Bash(go run:*)",
            "Bash(go mod:*)",
        ],
    ),
    "gradle": StackToolset(
        markers=["build.gradle", "build.gradle.kts"],
        allow=["Bash(./gradlew:*)", "Bash(gradle:*)"],
    ),
    "rust": StackToolset(
        markers=["Cargo.toml"],
        allow=[
            "Bash(cargo build:*)",
            "Bash(cargo test:*)",
            "Bash(cargo run:*)",
            "Bash(cargo clippy:*)",
        ],
    ),
}

# Every stack's build/test tools, for profiles that pre-approve all stacks
ALL_STACK_ALLOW = [entry for toolset in STACK_TOOLS.values() for entry in toolset.allow]

# Work-Infra profile: terraform/k8s/AWS-heavy repos.
# Per D-18/D-19: terraform mutate ops go in ask[], not allow[].
# Per D-19: domain-scoped WebFetch for docs access (curl is globally denied).
WORK_INFRA = PermissionProfile(
    name="work-infra",
    allow=[
        # Extra WebFetch domains not in global (global has WebFetch(*) but
        # listing domain-specific entries here is intentional for visibility)
        "WebFetch(domain:developer.hashicorp.com)",
        "WebFetch(domain:registry.terraform.io)",
        "WebFetch(domain:docs.aws.amazon.com)",
        # Terraform non-mutate extra (force-unlock is not in global)
        "Bash(terraform force-unlock:*)",
        # Infra-specific skill
        "Skill(terraform-mastery)",
    ],
    ask=[
        # Downgrade globally-allowed git push to require confirmation
        GIT_PUSH,
        # Terraform mutate ops: not in global allow/deny, but require explicit ask
        "Bash(terraform apply:*)",
        "Bash(terraform import:*)",
        # Container/orchestration mutate ops
        *DOCKER_WRITE,
        "Bash(kubectl apply:*)",
        "Bash(kubectl delete:*)",
        "Bash(kubectl patch:*)",
        "Bash(helm install:*)",
        "Bash(helm upgrade:*)",
        "Bash(helm rollback:*)",
    ],
)

# Work-App profile: application repos (JS/Go/Python/Kotlin/Rust).
# Base allow has docker read-only and make. Stack tools are merged at generation time.
WORK_APP = PermissionProfile(
    name="work-app",
    allow=[
        # Base tooling present in all app repos
        MAKE,
        *DOCKER_READ,
    ],
    ask=[
        # Downgrade globally-allowed git push to require confirmation
        GIT_PUSH,
        # Publishing operations require explicit confirmation
        "Bash(npm publish:*)",
        "Bash(cargo publish:*)",
    ],
)

# Own profile: personal repos — near-global freedom.
# Per D-21/D-22: broad permissions, git push stays auto-approved (no ask[]).
OWN = PermissionProfile(
    name="own",
    # Every stack, make, and docker read+write (own repos have full trust)
    allow=[*ALL_STACK_ALLOW, MAKE, *DOCKER_READ, *DOCKER_WRITE],
    ask=[],  # D-22: no restrictions beyond global deny for personal repos
)

# GLOBAL profile: manages ~/.claude/settings.json itself.
# Reachable only via `init-permissions global ...` — never returned by detect_profile().
# The allow list mirrors ~/.claude/settings.json as the dotfiles repo ships it; global sync
# rewrites that file from here, so an entry missing below is an entry sync would remove.
GLOBAL = PermissionProfile(
    name="global",
    allow=[
        # Bash build/test/run tools
        *(entry for entry in ALL_STACK_ALLOW if entry != NPX),
        MAKE,
        *DOCKER_READ,
        *DOCKER_WRITE,
        # Skills
        "Skill(claude-skill-auditor)",
        "Skill(lessons-learned)",
        # WebFetch domains
        "WebFetch(domain:apilist.tronscan.org)",
        "WebFetch(domain:gallery.ecr.aws)",
        "WebFetch(domain:pfisterer.dev)",
        "WebFetch(domain:repost.aws)",
        "WebFetch(domain:tronscan.org)",
        "WebFetch(domain:www.solutiontoolkit.com)",
        # Lets Claude read the global ~/.claude config tree
        GLOBAL_CONFIG_READ,
    ],
    ask=[],
)

# All profiles keyed by name for lookup.
PROFILES: dict[str, PermissionProfile] = {
    "work-infra": WORK_INFRA,
    "work-app": WORK_APP,
    "own": OWN,
    "global": GLOBAL,
}


def get_profile(name: str) -> PermissionProfile:
    """Return a PermissionProfile by its name identifier.

    Args:
        name: One of 'work-infra', 'work-app', 'own', or 'global'.

    Raises:
        KeyError: If the profile name is not recognized.
    """
    if name not in PROFILES:
        available = ", ".join(sorted(PROFILES.keys()))
        raise KeyError(f"Unknown profile '{name}'. Available: {available}")
    return PROFILES[name]
