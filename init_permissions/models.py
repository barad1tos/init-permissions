"""Pydantic models for an init-permissions permission profile system."""

from typing import Literal

from pydantic import BaseModel


class PermissionProfile(BaseModel):
    """A named permission profile with allow and ask arrays."""

    name: str
    """Profile identifier: 'work-infra', 'work-app', or 'own'."""

    allow: list[str]
    """Permission entries to add beyond the global allow list."""

    ask: list[str]
    """Permission entries to downgrade from global allow to explicit confirmation."""


class StackToolset(BaseModel):
    """File markers and allowed commands for a technology stack."""

    markers: list[str]
    """File names that indicate this stack is present (e.g., ['package.json'])."""

    allow: list[str]
    """Permission entries to add when this stack is detected."""


class RepoConfig(BaseModel):
    """Per-repo customization loaded from .claude/repo-config.json."""

    webfetch_domains: list[str] = []
    """Domains to allow via WebFetch(domain:X) entries."""

    mcp_tools: list[str] = []
    """MCP tool entries to add directly to allow[]."""

    bash_commands: list[str] = []
    """Bash permission entries to add directly to allow[]."""

    skills: list[str] = []
    """Skill permission entries to add directly to allow[]."""

    # 'global' deliberately excluded — global profile is CLI-only.
    profile: Literal["work-infra", "work-app", "own"] | None = None
    """Persistent profile override. When set, takes precedence over path-based detection."""


class GeneratedSettings(BaseModel):
    """Complete generated settings.json content."""

    schema_url: str = "https://json.schemastore.org/claude-code-settings.json"
    allow: list[str]
    ask: list[str]

    def to_settings_dict(self) -> dict:
        """Convert to a settings.json-compatible dictionary."""
        result: dict = {
            "$schema": self.schema_url,
            "permissions": {"allow": sorted(self.allow)},
        }
        if self.ask:
            result["permissions"]["ask"] = sorted(self.ask)
        return result
