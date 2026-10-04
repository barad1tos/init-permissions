"""Auto-detection logic for permission profile assignment.

Detection order (per D-08):
1. Path-first: an own prefix from config.toml -> always 'own'
2. Path-second: a work prefix -> inspect contents for Infra vs. App
3. Fallback: any other path -> 'own'

Work-Infra detection (per D-09/D-11):
- Primary: *.tf files within 3 directory levels
- Secondary: terraform modules in sub-dirs as an additional signal
"""

from pathlib import Path

from init_permissions.layout import classify
from init_permissions.profiles import STACK_TOOLS


def detect_profile(repo_path: Path) -> str:
    """Detect the permission profile for a repository.

    Uses path-first detection: own prefixes from config.toml always map to 'own'.
    Work prefixes are inspected for *.tf files to distinguish Work-Infra from Work-App.

    Args:
        repo_path: Path to the repository root directory.

    Returns:
        Profile name: 'work-infra', 'work-app', or 'own'.
    """
    if classify(repo_path) == "work":
        if _has_terraform_files(repo_path):
            return "work-infra"
        return "work-app"

    # Own prefixes and paths outside the configured layout
    return "own"


def detect_stacks(repo_path: Path) -> list[str]:
    """Detect technology stacks present in a repository.

    Checks the root directory for marker files defined in STACK_TOOLS.
    Used by Work-App profile to add only relevant build tool permissions.

    Args:
        repo_path: Path to the repository root directory.

    Returns:
        List of stack identifiers (e.g., ['node', 'python']) for detected stacks.
    """
    detected: list[str] = []
    for stack_name, toolset in STACK_TOOLS.items():
        for marker in toolset.markers:
            if (repo_path / marker).exists():
                detected.append(stack_name)
                break  # One marker match per stack is enough
    return detected


def _has_terraform_files(repo_path: Path) -> bool:
    """Check if a repository contains Terraform configuration files.

    Searches for *.tf files within 3 directory levels of the repo root
    (per D-09). Also checks for terraform module subdirectories as a
    secondary signal (per D-11).

    Args:
        repo_path: Path to the repository root directory.

    Returns:
        True if Terraform files are found within the depth limit.
    """
    # Primary check: *.tf files within 3 directory levels
    # depth <= 4 because the file path itself counts as one part
    for tf_file in repo_path.rglob("*.tf"):
        depth = len(tf_file.relative_to(repo_path).parts)
        if depth <= 4:
            return True

    # Secondary check: terraform module directories (per D-11)
    # Look for directories named 'modules' or containing 'terraform' in name
    for candidate in repo_path.iterdir():
        if candidate.is_dir() and candidate.name in ("modules", "terraform"):
            # Check one level deeper for .tf files
            for tf_file in candidate.rglob("*.tf"):
                depth = len(tf_file.relative_to(repo_path).parts)
                if depth <= 5:
                    return True

    return False
