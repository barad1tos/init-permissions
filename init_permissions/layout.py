"""Map repository paths to profile families using the user's checkout layout.

Repos live under one root as ``<git_root>/<host>/<owner>/<repo>``. The config file lists
which ``host[/owner]`` prefixes hold your own repos and which hold work repos:

    git_root = "~/git"

    [profiles]
    own = ["github.com"]
    work = ["git.example.com"]

The longest matching prefix wins, so ``work = ["github.com/acme"]`` can carve an employer's
GitHub org out of ``own = ["github.com"]``. Without a config file nothing is managed.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

Family = Literal["own", "work"]

CONFIG_ENV_VAR = "INIT_PERMISSIONS_CONFIG"


class LayoutProfiles(BaseModel):
    """Path prefixes, relative to git_root, for each profile family."""

    own: list[str] = []
    work: list[str] = []


class Layout(BaseModel):
    """Parsed config.toml: where checkouts live and which prefixes map to which family."""

    git_root: Path
    profiles: LayoutProfiles = LayoutProfiles()

    def classify(self, repo_path: Path) -> Family | None:
        """Return the family for repo_path, or None when the path is not managed."""
        relative = _relative_parts(repo_path, self.git_root)
        if relative is None:
            return None
        matches: list[tuple[int, Family]] = []
        families: tuple[tuple[Family, list[str]], ...] = (("own", self.profiles.own), ("work", self.profiles.work))
        for family, prefixes in families:
            for prefix in prefixes:
                prefix_parts = Path(prefix).parts
                # The prefix must name a directory above the repo, never the repo root itself
                if len(relative) > len(prefix_parts) and relative[: len(prefix_parts)] == prefix_parts:
                    matches.append((len(prefix_parts), family))
        # Longest prefix wins; a prefix listed under both families resolves to the stricter "work"
        return max(matches)[1] if matches else None

    def managed_roots(self) -> list[Path]:
        """Directories that bulk commands scan for repos, one per configured prefix."""
        prefixes = [*self.profiles.own, *self.profiles.work]
        return [self.git_root / prefix for prefix in prefixes]


def config_path() -> Path:
    """Location of config.toml: $INIT_PERMISSIONS_CONFIG, else the XDG config dir."""
    if override := os.environ.get(CONFIG_ENV_VAR):
        return Path(override).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "init-permissions" / "config.toml"


def load_layout() -> Layout | None:
    """Read config.toml; None when it does not exist.

    Raises:
        ValueError: The file exists but is not valid TOML or does not match the schema.
    """
    path = config_path()
    if not path.is_file():
        return None
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        layout = Layout.model_validate(raw)
    except (tomllib.TOMLDecodeError, ValidationError) as error:
        raise ValueError(f"Invalid init-permissions config {path}: {error}") from error
    return layout.model_copy(update={"git_root": layout.git_root.expanduser()})


def classify(repo_path: Path) -> Family | None:
    """Family of repo_path under the current config, or None when unmanaged or unconfigured."""
    layout = load_layout()
    return layout.classify(repo_path) if layout else None


def is_managed(repo_path: Path) -> bool:
    """Whether repo_path falls under a configured own or work prefix."""
    return classify(repo_path) is not None


def _relative_parts(repo_path: Path, git_root: Path) -> tuple[str, ...] | None:
    try:
        return repo_path.resolve().relative_to(git_root.resolve()).parts
    except ValueError:
        return None
