"""Permission profile definitions for Work-Infra, Work-App, and Own repo categories.

Per design decisions:
- D-13: No deny entries in profiles — global deny is the unbreakable floor.
- D-18: No terraform mutate ops in allow[]. Mutate stays in ask[].
- D-22: Own profile has empty ask[] — git push stays auto-approved.
- D-19: Use WebFetch(domain:X) instead of Bash(curl *) — curl is globally denied.

Note: GLOBAL is reachable only via `init-permissions global ...` — never returned
by detect_profile(). It manages ~/.claude/settings.json itself.
"""

from init_permissions.models import PermissionProfile, StackToolset

# Stack detection: maps stack identifier to file markers and allowed commands.
# Used by Work-App profile to add only relevant build tools per detected stack.
STACK_TOOLS: dict[str, StackToolset] = {
    "node": StackToolset(
        markers=["package.json"],
        allow=["Bash(npm:*)", "Bash(npx:*)", "Bash(yarn:*)", "Bash(pnpm:*)"],
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
        "Bash(git push:*)",
        # Terraform mutate ops: not in global allow/deny, but require explicit ask
        "Bash(terraform apply:*)",
        "Bash(terraform import:*)",
        # Container/orchestration mutate ops
        "Bash(docker build:*)",
        "Bash(docker push:*)",
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
        "Bash(make:*)",
        "Bash(docker ps:*)",
        "Bash(docker images:*)",
        "Bash(docker logs:*)",
    ],
    ask=[
        # Downgrade globally-allowed git push to require confirmation
        "Bash(git push:*)",
        # Publishing operations require explicit confirmation
        "Bash(npm publish:*)",
        "Bash(cargo publish:*)",
    ],
)

# Own profile: personal repos — near-global freedom.
# Per D-21/D-22: broad permissions, git push stays auto-approved (no ask[]).
OWN = PermissionProfile(
    name="own",
    allow=[
        # Node stack
        "Bash(npm:*)",
        "Bash(npx:*)",
        "Bash(yarn:*)",
        "Bash(pnpm:*)",
        # Python stack
        "Bash(pytest:*)",
        # Go stack
        "Bash(go build:*)",
        "Bash(go test:*)",
        "Bash(go run:*)",
        "Bash(go mod:*)",
        # Rust stack
        "Bash(cargo build:*)",
        "Bash(cargo test:*)",
        "Bash(cargo run:*)",
        "Bash(cargo clippy:*)",
        # JVM stack
        "Bash(./gradlew:*)",
        "Bash(gradle:*)",
        # Build utilities
        "Bash(make:*)",
        # Docker read+write (own repos have full trust)
        "Bash(docker ps:*)",
        "Bash(docker images:*)",
        "Bash(docker logs:*)",
        "Bash(docker build:*)",
        "Bash(docker push:*)",
    ],
    ask=[],  # D-22: no restrictions beyond global deny for personal repos
)

# GLOBAL profile: manages ~/.claude/settings.json itself.
# Reachable only via `init-permissions global ...` — never returned by detect_profile().
# The allow list mirrors the current contents of ~/.claude/settings.json plus the
# new Read(~/.claude/**) entry that lets Claude inspect the global config.
GLOBAL = PermissionProfile(
    name="global",
    allow=[
        # Bash build/test/run tools
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
        "Bash(npx:*)",
        "Bash(pnpm:*)",
        "Bash(pytest:*)",
        "Bash(yarn:*)",
        # Skills
        "Skill(gsd:debug)",
        "Skill(gsd:quick)",
        "Skill(lessons-learned)",
        # WebFetch domains
        "WebFetch(domain:gallery.ecr.aws)",
        "WebFetch(domain:pfisterer.dev)",
        "WebFetch(domain:repost.aws)",
        "WebFetch(domain:www.solutiontoolkit.com)",
        # NEW: lets Claude read the global ~/.claude config tree
        "Read(~/.claude/**)",
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
