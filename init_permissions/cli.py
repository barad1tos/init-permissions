"""Interactive CLI for generating and checking per-project .claude/settings.json files.

Implements D-04 (interactive flow), D-05 (verbose preview), D-10 (dry-run),
D-16 (non-interactive mode), DET-01/02/03 (check subcommand).

Subcommands:
 generate Generate .claude/settings.json for a repository (interactive)
 check Verify permissions health; outputs warnings for missing or drifted files
 validate Audit permissions against expected profile (single or bulk)
 sync Regenerate settings.json across all managed repos
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from init_permissions.detector import detect_stacks
from init_permissions.generator import (
    generate_settings,
    resolve_profile,
    write_settings,
)
from init_permissions.models import RepoConfig
from init_permissions.validator import ValidationResult, Verdict

if TYPE_CHECKING:
    from init_permissions.sync import Hazard, SyncResult


@click.group()
def cli() -> None:
    """Permission management for per-project .claude/settings.json."""


def _show_settings_diff(repo_path: Path, settings_dict: dict[str, object]) -> None:
    """Display a unified diff between existing and proposed settings.json."""
    existing_path = repo_path / ".claude" / "settings.json"
    if not existing_path.exists():
        return

    existing_text = existing_path.read_text(encoding="utf-8").splitlines(keepends=True)
    new_text = (json.dumps(settings_dict, indent=2) + "\n").splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            existing_text,
            new_text,
            fromfile=".claude/settings.json (current)",
            tofile=".claude/settings.json (proposed)",
        )
    )
    if not diff_lines:
        click.echo("\nNo changes from existing settings.")
        return

    click.echo("\nDiff from existing settings:")
    for line in diff_lines:
        if line.startswith("+") and not line.startswith("+++"):
            click.echo(click.style(line.rstrip(), fg="green"))
        elif line.startswith("-") and not line.startswith("---"):
            click.echo(click.style(line.rstrip(), fg="red"))
        else:
            click.echo(line.rstrip())


def _announce_profile_choice(
    cli_profile: str | None,
    resolved_profile: str,
    repo_config: RepoConfig | None,
) -> str:
    """Print how the profile was chosen and return the profile name to use."""
    if cli_profile is None:
        has_override = repo_config is not None and repo_config.profile is not None
        label = "Resolved profile" if has_override else "Detected profile"
        suffix = " (from repo-config.json)" if has_override else ""
        click.echo(f"{label}: {click.style(resolved_profile, fg='cyan', bold=True)}{suffix}")
        return resolved_profile

    click.echo(f"Using profile: {click.style(cli_profile, fg='cyan', bold=True)} (override)")
    already_persisted = repo_config is not None and repo_config.profile == cli_profile
    if not already_persisted:
        click.echo(
            click.style(
                f'  Hint: to make this persistent, add `"profile": "{cli_profile}"` to .claude/repo-config.json',
                fg="bright_black",
            )
        )
    return cli_profile


@cli.command()
@click.argument(
    "path",
    default=".",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
)
@click.option(
    "--profile",
    type=click.Choice(["work-infra", "work-app", "own"]),
    default=None,
    help="Override auto-detected profile.",
)
@click.option("--dry-run", is_flag=True, help="Preview without writing any file.")
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Skip confirmation prompts and write directly.",
)
def generate(path: str, profile: str | None, dry_run: bool, non_interactive: bool) -> None:
    """Generate .claude/settings.json for a repository.

    Detects the permission profile from PATH (defaults to current directory),
    merges with any .claude/repo-config.json present, shows a verbose plan,
    and writes the result after confirmation.
    """
    repo_path = Path(path)

    # Step 1: Resolve profile (CLI flag wins; otherwise honor repo-config override or detect)
    resolved_profile, repo_config = resolve_profile(repo_path)
    profile_name = _announce_profile_choice(profile, resolved_profile, repo_config)

    # Step 2: Detect stacks for work-app and show them
    if profile_name == "work-app":
        stacks = detect_stacks(repo_path)
        if stacks:
            click.echo(f"Detected stacks: {', '.join(stacks)}")
        else:
            click.echo("Detected stacks: none")

    # Step 4: Generate settings
    settings = generate_settings(profile_name, repo_path, repo_config)
    settings_dict = settings.to_settings_dict()
    permissions = settings_dict.get("permissions", {})
    allow_entries: list[str] = permissions.get("allow", [])
    ask_entries: list[str] = permissions.get("ask", [])

    # Step 5: Show verbose plan (per D-05)
    click.echo(f"\nProfile: {click.style(profile_name, fg='cyan', bold=True)}")
    click.echo(f"Allow ({len(allow_entries)} entries):")
    for entry in allow_entries:
        click.echo(click.style(f"  + {entry}", fg="green"))
    click.echo(f"Ask ({len(ask_entries)} entries):")
    for entry in ask_entries:
        click.echo(click.style(f"  ? {entry}", fg="yellow"))

    # Step 6: Show unified diff against existing settings.json if present
    _show_settings_diff(repo_path, settings_dict)

    # Step 7: Confirm and write
    if dry_run:
        click.echo("\nDry run — no files written.")
        return

    if not non_interactive:
        click.confirm("\nWrite .claude/settings.json?", abort=True)

    written_path = write_settings(settings, repo_path)
    click.echo(f"Written to {written_path}")


@cli.command()
@click.argument(
    "path",
    default=".",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
)
def check(path: str) -> None:
    """Check permissions health for a repository.

    Outputs warnings to stdout when .claude/settings.json is missing
    or when the permissions have drifted from the expected profile.
    Outputs nothing and exits 0 for clean repos and non-managed paths.
    """
    from init_permissions.checker import run_check

    result = run_check(Path(path))
    for warning in result.warnings:
        click.echo(warning)


@cli.command()
@click.argument(
    "path",
    default=".",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
)
@click.option(
    "--all",
    "validate_all",
    is_flag=True,
    default=False,
    help="Validate all managed repos under Developer/Work/ and Developer/Own/.",
)
def validate(path: str, *, validate_all: bool) -> None:
    """Validate permissions against the expected profile.

    Returns PASS/WARN/FAIL with profile name and specific issues.
    Use --all to bulk-validate all managed repos in one command.
    """
    from init_permissions.validator import bulk_validate, run_validate

    if validate_all:
        _print_bulk_validation(bulk_validate())
    else:
        _print_single_validation(run_validate(Path(path)))


_VERDICT_STYLE = {
    Verdict.PASS: "green",
    Verdict.WARN: "yellow",
    Verdict.FAIL: "red",
}
_VERDICT_SYMBOL = {Verdict.PASS: "✓", Verdict.WARN: "⚠", Verdict.FAIL: "✗"}
_DOT_SEPARATOR = "  ·  "
_STYLE_BOLD_RED = "bold red"


def _print_bulk_validation(results: list[ValidationResult]) -> None:
    """Print bulk validation results as a rich summary table."""
    console = Console()

    if not results:
        console.print("No managed repos found.")
        return

    pass_count = sum(1 for r in results if r.verdict == Verdict.PASS)
    warn_count = sum(1 for r in results if r.verdict == Verdict.WARN)
    fail_count = sum(1 for r in results if r.verdict == Verdict.FAIL)
    total = len(results)

    summary = Text.assemble(
        (f"{pass_count}/{total} PASS", "bold green"),
        (_DOT_SEPARATOR, "dim"),
        (f"{warn_count} WARN", "bold yellow"),
        (_DOT_SEPARATOR, "dim"),
        (f"{fail_count} FAIL", _STYLE_BOLD_RED),
    )
    console.print(Panel(summary, title="Validation summary", border_style="cyan", expand=False))

    table = Table(box=box.SIMPLE, show_lines=False, expand=True)
    table.add_column("Repo", style="bold", no_wrap=True, overflow="ellipsis", ratio=3, min_width=20)
    table.add_column("Profile", no_wrap=True, min_width=10)
    table.add_column("Verdict", no_wrap=True, min_width=8)
    table.add_column("Issues", overflow="ellipsis", no_wrap=True, ratio=5, min_width=20)

    for result in results:
        repo_name = Path(result.repo_path).name
        issue_summary = "; ".join(result.issues) if result.issues else ""
        verdict_text = Text(result.verdict.value, style=_VERDICT_STYLE[result.verdict])
        table.add_row(repo_name, result.profile, verdict_text, issue_summary)

    console.print(table)

    failed = [r for r in results if r.verdict != Verdict.PASS]
    if not failed:
        return

    console.print(f"\n[bold]Details[/bold] ({len(failed)} repos with issues)")
    for result in failed:
        repo_name = Path(result.repo_path).name
        style = _VERDICT_STYLE[result.verdict]
        header = Text.assemble(
            (f"{result.verdict.value} ", f"bold {style}"),
            (repo_name, "bold"),
            (f"  ({result.profile})", "dim"),
        )
        console.print(Panel(_render_issues(result.issues), title=header, border_style=style, expand=True, padding=(0, 1)))


_LIST_REPR_RE = __import__("re").compile(r"^(.*?:\s*)(\[.*\])\s*$", __import__("re").DOTALL)


def _try_extract_list(issue: str) -> tuple[str, list[str]] | None:
    """If `issue` ends with a Python list repr, return (prefix, items). Else None."""
    import ast

    match = _LIST_REPR_RE.match(issue)
    if not match:
        return None
    try:
        items = ast.literal_eval(match.group(2))
    except (ValueError, SyntaxError):
        return None
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        return None
    return match.group(1).rstrip(), items


def _render_issues(issues: list[str]) -> Table:
    """Render issues as a borderless grid; expand list-repr tails into bullets."""
    grid = Table.grid(padding=(0, 1), expand=True)
    grid.add_column(overflow="fold")
    for issue in issues:
        extracted = _try_extract_list(issue)
        if extracted is None:
            grid.add_row(Text(f"• {issue}"))
            continue
        prefix, items = extracted
        grid.add_row(Text(f"• {prefix}", style="bold"))
        sub = Table.grid(padding=(0, 2), expand=False)
        sub.add_column(overflow="fold")
        sub.add_column(overflow="fold")
        midpoint = (len(items) + 1) // 2
        left, right = items[:midpoint], items[midpoint:]
        for i in range(midpoint):
            left_item = left[i]
            right_item = right[i] if i < len(right) else ""
            sub.add_row(
                Text(f"  {left_item}", style="cyan"),
                Text(f"  {right_item}", style="cyan") if right_item else "",
            )
        grid.add_row(sub)
    return grid


def _print_single_validation(result: ValidationResult) -> None:
    """Print a single-repo validation result with a verdict symbol."""
    console = Console()
    symbol = _VERDICT_SYMBOL[result.verdict]
    style = _VERDICT_STYLE[result.verdict]
    console.print(f"[{style}]{symbol} {result.verdict.value}[/] ({result.profile or 'non-managed'})")
    for issue in result.issues:
        console.print(f"  - {issue}")


@cli.command()
@click.option(
    "--check",
    "dry_run",
    is_flag=True,
    default=False,
    help="Preview what would change without modifying any files.",
)
def sync(*, dry_run: bool) -> None:
    """Sync settings.json for all managed repos from current profiles.

    Default: regenerate all repos. With --check: preview changes only.
    """
    from init_permissions.sync import bulk_sync

    plan = bulk_sync(dry_run=dry_run)

    _print_sync_hazards(plan.hazards)
    _print_sync_excluded(plan.excluded)

    if not plan.results:
        click.echo("No managed repos to sync.")
        return

    _print_sync_results_table(plan.results, dry_run=dry_run)
    _print_sync_summary(plan.results, dry_run=dry_run)


def _print_sync_hazards(hazards: list[Hazard]) -> None:
    """Print the hazards' safety banner above the per-repo results."""
    if not hazards:
        return

    console = Console()
    blocks = sum(1 for h in hazards if h.severity == "BLOCK")
    warns = sum(1 for h in hazards if h.severity == "WARN")

    parts: list[Text] = []
    for hazard in hazards:
        color = "red" if hazard.severity == "BLOCK" else "yellow"
        entry = Text()
        entry.append(f"{hazard.severity}  ", style=f"bold {color}")
        entry.append(hazard.category, style="bold")
        if hazard.repo is not None:
            entry.append(f"\n  repo: {hazard.repo}", style="dim")
        entry.append(f"\n  {hazard.message}")
        if hazard.explanation:
            for line in hazard.explanation.splitlines():
                entry.append(f"\n  {line}", style="bright_black")
        parts.append(entry)

    body = Text("\n\n").join(parts)
    title = Text(f"⛔ {blocks} BLOCK · {warns} WARN — review before apply", style=_STYLE_BOLD_RED)
    console.print(Panel(body, title=title, border_style="red", expand=True))


def _print_sync_excluded(excluded: list[Path]) -> None:
    """Print the self-write-excluded repos list."""
    if not excluded:
        return
    console = Console()
    body = Text("\n".join(f"• {repo.name}  (settings.json resolves to global source)" for repo in excluded))
    console.print(
        Panel(
            body,
            title=Text(f"Excluded from sync ({len(excluded)} self-write repos)", style="bold yellow"),
            border_style="yellow",
            expand=False,
        )
    )


def _sync_row_for(result: SyncResult, *, dry_run: bool) -> tuple[str, Text, str] | None:
    """Return (repo, status, detail) for a single result, or None to skip."""
    from init_permissions.sync import SyncAction

    repo_name = Path(result.repo_path).name
    action = result.action

    if action == SyncAction.UNCHANGED:
        return repo_name, Text("unchanged", style="dim"), ""
    if action in (SyncAction.UPDATED, SyncAction.WOULD_UPDATE):
        verb = "would update" if dry_run else "updated"
        return repo_name, Text(verb, style="cyan"), f"-{result.entries_removed}/+{result.entries_added} entries"
    if action in (SyncAction.CREATED, SyncAction.WOULD_CREATE):
        verb = "would create" if dry_run else "created"
        return repo_name, Text(verb, style="green"), ""
    if action == SyncAction.SKIPPED:
        return repo_name, Text("skipped", style="dim"), "non-managed"
    if action == SyncAction.ERROR:
        return repo_name, Text("ERROR", style=_STYLE_BOLD_RED), result.error
    return None


def _print_sync_results_table(results: list[SyncResult], *, dry_run: bool) -> None:
    """Render per-repo sync results as a rich table."""
    console = Console()
    table = Table(box=box.SIMPLE, show_lines=False, expand=True)
    table.add_column("Repo", style="bold", no_wrap=True, overflow="ellipsis", max_width=40)
    table.add_column("Status", no_wrap=True)
    table.add_column("Detail", overflow="ellipsis", no_wrap=True)

    for result in results:
        row = _sync_row_for(result, dry_run=dry_run)
        if row is not None:
            table.add_row(*row)

    console.print(table)


@cli.group("global")
def global_group() -> None:
    """Manage ~/.claude/settings.json (the global profile)."""


_DEFAULT_GLOBAL_PATH = Path.home() / ".claude" / "settings.json"


@global_group.command("sync")
@click.option(
    "--check",
    "dry_run",
    is_flag=True,
    default=False,
    help="Preview without writing.",
)
@click.option(
    "--global-path",
    "global_path",
    type=click.Path(path_type=Path),
    default=_DEFAULT_GLOBAL_PATH,
    hidden=True,
)
def global_sync_cmd(*, dry_run: bool, global_path: Path) -> None:
    """Sync ~/.claude/settings.json against the GLOBAL profile."""
    from init_permissions.global_sync import run_global_sync
    from init_permissions.sync import SyncAction

    result = run_global_sync(global_path, dry_run=dry_run)
    console = Console()

    color_map = {
        SyncAction.CREATED: "green",
        SyncAction.WOULD_CREATE: "green",
        SyncAction.UPDATED: "cyan",
        SyncAction.WOULD_UPDATE: "cyan",
        SyncAction.UNCHANGED: "dim",
        SyncAction.ERROR: "red",
    }
    color = color_map.get(result.action, "white")
    detail = f"-{result.entries_removed}/+{result.entries_added} entries"
    body = Text.assemble(
        (f"{result.action.value}  ", f"bold {color}"),
        (detail, color),
    )
    if result.action == SyncAction.ERROR:
        body.append(f"\n{result.error}", style="red")
    console.print(Panel(body, title=Text(str(global_path), style="bold"), border_style=color, expand=False))


@global_group.command("check")
@click.option(
    "--global-path",
    "global_path",
    type=click.Path(path_type=Path),
    default=_DEFAULT_GLOBAL_PATH,
    hidden=True,
)
def global_check_cmd(global_path: Path) -> None:
    """Report drift in ~/.claude/settings.json. Silent when clean."""
    from init_permissions.global_sync import run_global_check

    for warning in run_global_check(global_path):
        click.echo(warning)


@global_group.command("validate")
@click.option(
    "--global-path",
    "global_path",
    type=click.Path(path_type=Path),
    default=_DEFAULT_GLOBAL_PATH,
    hidden=True,
)
def global_validate_cmd(global_path: Path) -> None:
    """Validate ~/.claude/settings.json against the GLOBAL profile."""
    from init_permissions.global_sync import run_global_validate

    _print_single_validation(run_global_validate(global_path))


def _print_sync_summary(results: list[SyncResult], *, dry_run: bool) -> None:
    """Print the final totals line for bulk sync."""
    from init_permissions.sync import SyncAction

    updated = sum(1 for r in results if r.action in (SyncAction.UPDATED, SyncAction.WOULD_UPDATE))
    created = sum(1 for r in results if r.action in (SyncAction.CREATED, SyncAction.WOULD_CREATE))
    unchanged = sum(1 for r in results if r.action == SyncAction.UNCHANGED)
    errors = sum(1 for r in results if r.action == SyncAction.ERROR)

    mode_label = "Would sync" if dry_run else "Synced"
    summary = Text.assemble(
        (f"{mode_label}: ", "bold"),
        (f"{updated} updated", "cyan"),
        (_DOT_SEPARATOR, "dim"),
        (f"{created} created", "green"),
        (_DOT_SEPARATOR, "dim"),
        (f"{unchanged} unchanged", "dim"),
        (_DOT_SEPARATOR, "dim"),
        (f"{errors} errors", "red" if errors else "dim"),
    )
    Console().print(Panel(summary, border_style="cyan", expand=False))
