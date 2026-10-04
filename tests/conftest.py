"""Shared fixtures: every test runs against an isolated checkout layout rooted at tmp_path."""

from __future__ import annotations

from pathlib import Path

import pytest

from init_permissions.layout import CONFIG_ENV_VAR


@pytest.fixture(autouse=True)
def git_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config.toml at tmp_path: github.com/* is own, git.example.com/* is work."""
    config = tmp_path / "init-permissions.toml"
    config.write_text(
        f'git_root = "{tmp_path}"\n\n[profiles]\nown = ["github.com"]\nwork = ["git.example.com"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_ENV_VAR, str(config))
    return tmp_path
