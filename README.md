# init-permissions

Per-project permission management for Claude Code (`.claude/settings.json`).

Detects the repo profile, generates settings with correct `allow[]` and `ask[]`,
deduplicates against the global `~/.claude/settings.json`, keeps everything in sync,
and prompts interactively when drift is detected at session start.

---

## Quick Start

```bash
perms-gen       # Generate settings.json for current repo (interactive)
perms-check     # Validate current repo → PASS / WARN / FAIL
perms-all       # Bulk-validate all managed repos
perms-diff      # Preview what sync would change (dry-run)
perms-sync      # Apply sync to all repos
```

Shell aliases defined in `zsh/.zsh_aliases`. Full equivalents:

```bash
perms generate .          # same as perms-gen
perms validate .          # same as perms-check
perms validate --all      # same as perms-all
perms sync --check        # same as perms-diff
perms sync                # same as perms-sync
```

---

## How It Works (End-to-End Flow)

```
Session start
     │
     ▼
permissions-check.sh          ← SessionStart hook in ~/.claude/hooks/
     │  reads cwd from hook input JSON
     │  skips non-managed paths silently
     │  checks ~/.claude/settings.json exists
     │  runs weekly drift check (cached)
     │
     ▼
init-permissions.py check     ← Python CLI (scripts/init-permissions.py)
     │  calls checker.py → run_check()
     │  returns MISSING: / DRIFT: / FIX: / CHECK: lines
     │
     ▼
[PERMISSIONS — ACTION REQUIRED]  ← injected as systemMessage to Claude
     │
     ▼
Claude presents issue + AskUserQuestion
     │  "Fix now?" → runs perms-gen / perms-diff
     │  "Skip"    → proceeds with session
```

On a clean repo — hook exits silently, no interruption.

---

## Permission Layers

There are three layers, from broadest to narrowest:

| Layer                 | File                            | Scope                           | Owner            |
|-----------------------|---------------------------------|---------------------------------|------------------|
| **Global allow**      | `~/.claude/settings.json`       | All sessions everywhere         | You (manually)   |
| **Per-project allow** | `.claude/settings.json` in repo | This repo only                  | init-permissions |
| **Per-project ask**   | `.claude/settings.json` in repo | Downgrades from allow → confirm | init-permissions |

**Key rule:** Per-project entries deduplicate against global. If an entry is already in `~/.claude/settings.json allow[]`, it is silently removed from the per-project file to avoid redundancy.

There is no `deny[]` at the per-project level — global deny is the hard floor and is never touched by this tool.

---

## Key Concepts

### `allow[]`
Commands Claude can execute without asking. The per-project `allow[]` is a **superset** check:
validation passes if the actual file contains _at least_ the expected entries (extra entries are
harmless and produce WARN, not FAIL).

### `ask[]`
Commands that are globally allowed but **downgraded to require confirmation** for this repo.
Example: `git push` is globally allowed but `work-infra` puts it in `ask[]` to force
an explicit confirmation before pushing infra changes. `ask[]` is an **exact set match** —
any drift (extra or missing entries) is FAIL.

### Drift
When the actual `allow[]` or `ask[]` in a repo's `settings.json` no longer matches what the
generator would produce for that profile. Detected weekly by the SessionStart hook, reported
as `DRIFT:` warnings with fix commands.

### Dedup
Global entries are stripped from per-project `allow[]` at generation time. If you add something
to `~/.claude/settings.json` later, run `perms-sync` to remove the now-redundant per-project entry.

---

## Profiles

### `work-infra` — Terraform / AWS / Kubernetes repos

Detected when: a work prefix in `config.toml` + `*.tf` files found within 3 directory levels.

**ask[] (requires confirmation — 11 entries):**

| Command                    | Reason                                          |
|----------------------------|-------------------------------------------------|
| `Bash(git push:*)`         | Infra changes are irreversible — always confirm |
| `Bash(terraform apply:*)`  | Mutates cloud state                             |
| `Bash(terraform import:*)` | Mutates state file                              |
| `Bash(docker build:*)`     | Slow + may push to registry                     |
| `Bash(docker push:*)`      | Publishes image                                 |
| `Bash(kubectl apply:*)`    | Mutates cluster state                           |
| `Bash(kubectl delete:*)`   | Destructive                                     |
| `Bash(kubectl patch:*)`    | Mutates cluster state                           |
| `Bash(helm install:*)`     | Deploys to cluster                              |
| `Bash(helm upgrade:*)`     | Mutates running release                         |
| `Bash(helm rollback:*)`    | Mutates running release                         |

**allow[] (auto-approved — 5 entries):**
- `WebFetch(domain:developer.hashicorp.com)` — Terraform docs
- `WebFetch(domain:registry.terraform.io)` — Provider registry
- `WebFetch(domain:docs.aws.amazon.com)` — AWS docs
- `Bash(terraform force-unlock:*)` — Non-mutate unlock
- `Skill(terraform-mastery)` — Terraform skill

---

### `work-app` — Application repos (JS / Go / Python / Kotlin / Rust)

Detected when: a work prefix in `config.toml` + no `*.tf` files.

**ask[] (requires confirmation — 3 entries):**

| Command                 | Reason                                       |
|-------------------------|----------------------------------------------|
| `Bash(git push:*)`      | Work repos: always confirm before publishing |
| `Bash(npm publish:*)`   | Publishes package to registry                |
| `Bash(cargo publish:*)` | Publishes crate to crates.io                 |

**allow[] — base (4 entries) + stack-detected tools:**

Base (always included):
- `Bash(make:*)`, `Bash(docker ps:*)`, `Bash(docker images:*)`, `Bash(docker logs:*)`

Stack-detected (merged at generation time based on files found in the repo root):

| Stack    | Trigger file                                       | Added allow[] entries                                            |
|----------|----------------------------------------------------|------------------------------------------------------------------|
| `node`   | `package.json`                                     | `npm:*`, `npx:*`, `yarn:*`, `pnpm:*`                             |
| `python` | `pyproject.toml` / `setup.py` / `requirements.txt` | `pytest:*`                                                       |
| `go`     | `go.mod`                                           | `go build:*`, `go test:*`, `go run:*`, `go mod:*`                |
| `gradle` | `build.gradle` / `build.gradle.kts`                | `./gradlew:*`, `gradle:*`                                        |
| `rust`   | `Cargo.toml`                                       | `cargo build:*`, `cargo test:*`, `cargo run:*`, `cargo clippy:*` |

Multiple stacks can be detected simultaneously (e.g., a monorepo with both `package.json` and `go.mod`).

---

### `own` — Personal repos

Detected when the repo sits under an own prefix in `config.toml` (no content check needed).

**ask[] — empty.** Git push and everything else are auto-approved.

**allow[] (25 entries):** All stacks pre-included (node, Python, Go, Rust, Gradle), plus
`make:*`, docker read+write (build/push/ps/images/logs).

---

## Profile Detection

Detection is path-first. Checkouts are expected under one root as `<git_root>/<host>/<owner>/<repo>`,
and `~/.config/init-permissions/config.toml` (or the file named by `$INIT_PERMISSIONS_CONFIG`) says
which `host[/owner]` prefixes are own and which are work:

```toml
git_root = "~/git"

[profiles]
own = ["github.com"]
work = ["git.example.com", "github.com/acme"]
```

```
under an own prefix   →  own  (no content check)
under a work prefix   →  inspect for *.tf files
    found *.tf within 3 levels  →  work-infra
    no *.tf                     →  work-app
anything else         →  not managed: check / validate / sync skip it
```

The longest matching prefix wins, so `github.com/acme` above is work even though `github.com` is own.
A prefix listed under both families counts as work. Without a config file nothing is managed.

Work repos usually carry a team-owned `.claude/settings.json` with its own deny list, so the tool
never writes it there: the work profile goes into `.claude/settings.local.json`, the personal layer
Claude Code merges on top of the shared file. Own repos keep using `.claude/settings.json`. Either
way only `$schema` and `permissions` are replaced; other top-level keys stay.
`perms generate` still works on any path and falls back to `own`.

Override at generation time: `perms generate --profile=work-infra .`

---

## Customization: `repo-config.json`

Add `.claude/repo-config.json` to a repo to extend its generated settings:

```json
{
  "webfetch_domains": ["api.example.com"],
  "mcp_tools": ["mcp__jira__get_issue"],
  "bash_commands": ["Bash(./scripts/deploy.sh:*)"],
  "skills": ["Skill(terraform-mastery)"],
  "profile": "work-app"
}
```

All entries merge into `allow[]` and deduplicate against global settings.
This file is read at every `generate`, `check`, `validate`, and `sync` run — no need to re-run after editing.

### Persistent profile override: `profile`

The optional `profile` field (`"work-infra"` | `"work-app"` | `"own"`) overrides path-based detection across **every** subcommand. Use it when path detection picks the wrong profile for a repo — e.g. a work repo that contains `modules/*.tf` but should be treated as `work-app`, not `work-infra`. Without this field, `perms sync` would silently rewrite the file back to the path-detected profile on every run.

When an override is set, `validator` skips its profile-mismatch check entirely — the override IS the user's declared truth, not drift.

---

## SessionStart Hook

**File:** `~/.claude/hooks/permissions-check.sh`
**Registered in:** `~/.claude/settings.json` under `SessionStart`

### What it does

1. Reads `cwd` from the hook input JSON
2. Exits silently outside `git_root` (`/tmp`, `~/.claude`, etc.); inside it, the Python checker
   decides whether the repo is under a managed prefix
3. **Missing check** (always): if `.claude/settings.json` doesn't exist → warn immediately
4. **Drift check** (weekly): compares current settings against generator output
5. On any issue: wraps the checker output in `[PERMISSIONS — ACTION REQUIRED]` and outputs
   it as `{"systemMessage": ...}` — Claude reads this and must present + prompt before doing other work

### Cache

Drift checks are cached per-repo in `~/.claude/cache/permissions-check.json`.
Cache key = md5 of `cwd`. Default interval: 7 days.

Configure via `.claude/.planning/config.json`:
```json
{
  "workflow": {
    "permissions_check_interval": 7
  }
}
```
Set to `-1` to disable drift checks entirely (missing-file check always runs).

### Warning format (from checker.py)

```
MISSING: No .claude/settings.json for repo-name (detected profile: own)
FIX:     perms-gen  # or: perms generate /path/to/repo
CHECK:   perms-check  # or: perms validate /path/to/repo

DRIFT: allow[] missing 3 expected entries: ['Bash(pytest:*)', ...]
DRIFT: ask[] mismatch — extra: ['Bash(git push:*)']
FIX:   perms-diff  # preview changes  (or: perms sync --check)
FIX:   perms-gen   # regenerate this repo  (or: perms generate /path/to/repo)
CHECK: perms-check  # verify after fix  (or: perms validate /path/to/repo)
```

Claude's behavior when it receives `[PERMISSIONS — ACTION REQUIRED]`:
1. One-sentence plain-language summary of the issue
2. `AskUserQuestion` → "Fix now?" or "Skip"
3. If the user confirms: runs the FIX command, then verifies with CHECK

---

## Commands Reference

### `generate` — Create or update settings.json

```bash
perms generate [OPTIONS] [PATH]
perms-gen                          # shortcut for current dir
```

| Option              | Effect                                                    |
|---------------------|-----------------------------------------------------------|
| `--profile=NAME`    | Override auto-detection (`work-infra`, `work-app`, `own`) |
| `--dry-run`         | Show what would be written without writing                |
| `--non-interactive` | Skip confirmation prompt                                  |

**Flow:**
1. Auto-detect profile (or use `--profile`)
2. Detect stacks (work-app only)
3. Load `.claude/repo-config.json` if present
4. Generate settings: profile base + stack tools + repo-config
5. Deduplicate against global `~/.claude/settings.json allow[]`
6. Show the verbose plan (all allow/ask entries with +/? prefixes)
7. Show a unified diff against the existing settings.json
8. Confirm → write

---

### `check` — Health check (used by SessionStart hook)

```bash
perms check [PATH]
```

Outputs `MISSING:`/`DRIFT:`/`FIX:`/`CHECK:` warnings to stdout.
**Silent (no output) for clean repos and non-managed paths.**
Exit code 0 always — designed for hook use.

---

### `validate` — Audit against profile

```bash
perms validate [PATH]         # single repo
perms validate --all          # all managed repos
perms-check                   # shortcut (single, current dir)
perms-all                     # shortcut (all repos)
```

Returns a verdict for each repo:

| Verdict  | Meaning                                            |
|----------|----------------------------------------------------|
| **PASS** | Matches expected profile exactly                   |
| **WARN** | Extra `allow[]` entries (harmless customization)   |
| **FAIL** | Missing settings, ask[] drift, or profile mismatch |

**Profile mismatch** (`VALID-03`): when `ask[]` signature in the actual file matches a
_different_ profile than path detection suggests. Example: a work repo with
empty `ask[]` (looks like `own`) — reported as mismatch with both profiles named.

Bulk mode (`--all`) shows:
- Summary line: `N/total PASS, W WARN, F FAIL`
- Table with one row per repo
- Expanded details for all non-PASS repos

---

### `sync` — Bulk regenerate all repos

```bash
perms sync              # apply to all repos
perms sync --check      # dry-run preview only
perms-sync              # shortcut (apply)
perms-diff              # shortcut (dry-run)
```

For each managed repo:
- Regenerates `permissions` section from the current profile and stack detection
- **Preserves all other top-level keys** in settings.json (hooks, etc.)
- Reports: `updated`, `created`, `unchanged`, `error` per repo

---

## Architecture

```
scripts/init-permissions.py       ← entry point (thin wrapper around CLI)

init_permissions/
├── models.py       PermissionProfile, StackToolset, RepoConfig, GeneratedSettings
├── profiles.py     WORK_INFRA, WORK_APP, OWN constants; STACK_TOOLS; get_profile()
├── detector.py     detect_profile(), detect_stacks(), _has_terraform_files()
├── generator.py    generate_settings(), load_repo_config(), write_settings(),
│                   load_global_allow(), _merge_stack_tools(), _merge_repo_config()
├── checker.py      run_check() → CheckResult with MISSING:/DRIFT:/FIX:/CHECK: warnings
├── validator.py    run_validate() → ValidationResult (PASS/WARN/FAIL);
│                   bulk_validate(), identify_applied_profile()
├── sync.py         run_sync() → SyncResult; bulk_sync()
└── cli.py          click.group(): generate / check / validate / sync subcommands

~/.claude/hooks/permissions-check.sh   ← SessionStart hook
~/.claude/CLAUDE.md                     ← mandatory rule: act on [PERMISSIONS — ACTION REQUIRED]
```

### Data flow

```
detect_profile()  ─┐
detect_stacks()   ─┤
load_repo_config() ─┤→  generate_settings()  →  GeneratedSettings(allow, ask)
load_global_allow() ┘         │
                               │ dedup against global
                               ▼
                        write_settings()  →  .claude/settings.json
                               │
                        run_check()       →  CheckResult(warnings=[...])
                               │
                        run_validate()    →  ValidationResult(verdict, issues)
                               │
                        run_sync()        →  SyncResult(action, entries_added/removed)
```

---

## Tests

```bash
cd init-permissions && uv run pytest -v
```

The test suite covers 68 cases across:
- `test_models.py` — Pydantic schema validation
- `test_rules.py` — profile rule assertions (unit)
- `test_snapshot.py` — JSON output snapshots
- `test_checker.py` — 8 tests for run_check() (MISSING/DRIFT/clean/non-managed)
- `test_validator.py` — 10 tests for run_validate() and identify_applied_profile()
- `test_sync.py` — 11 tests for run_sync() and bulk_sync()

Run type checks:

```bash
cd init-permissions && uv run pyright .
# Expected: 0 errors, 0 warnings
```

---

## Entity Reference (for questions)

When asking about this system, use these names:

| Entity                | What it is                                                                             |
|-----------------------|----------------------------------------------------------------------------------------|
| **profile**           | One of `work-infra`, `work-app`, `own` — determines the permission template            |
| **allow[]**           | Entries Claude can execute without asking (per-project, on top of global)              |
| **ask[]**             | Entries downgraded from global allow to explicit confirmation                          |
| **global allow**      | `~/.claude/settings.json permissions.allow[]` — the shared baseline                    |
| **drift**             | When actual settings.json diverges from what the generator would produce               |
| **dedup**             | Removing per-project entries already present in global allow                           |
| **stack detection**   | Auto-detecting Node/Go/Python/Rust/Gradle from marker files                            |
| **profile detection** | Assigning work-infra/work-app/own from repo path and .tf presence                      |
| **profile mismatch**  | When ask[] signature matches a different profile than path detection says              |
| **repo-config**       | `.claude/repo-config.json` — per-repo customization (extra domains, tools)             |
| **SessionStart hook** | `permissions-check.sh` — runs at session start, outputs systemMessage                  |
| **systemMessage**     | Hook output format: `{"systemMessage": "..."}` injected into Claude context            |
| **checker**           | `checker.py` / `run_check()` — produces MISSING/DRIFT warnings                         |
| **validator**         | `validator.py` / `run_validate()` — produces PASS/WARN/FAIL verdict                    |
| **sync engine**       | `sync.py` / `run_sync()` — regenerates settings, preserves non-permission keys         |
| **generator**         | `generator.py` / `generate_settings()` — merges profile + stacks + repo-config + dedup |
| **CheckResult**       | Pydantic model: `{repo_path, profile, warnings[]}`                                     |
| **ValidationResult**  | Pydantic model: `{repo_path, profile, verdict, issues[], profile_mismatch, ...}`       |
| **SyncResult**        | Pydantic model: `{repo_path, action, profile, entries_added, entries_removed, error}`  |
| **SyncAction**        | Enum: UNCHANGED / UPDATED / CREATED / SKIPPED / WOULD_UPDATE / WOULD_CREATE / ERROR    |
| **Verdict**           | Enum: PASS / WARN / FAIL                                                               |
| **cache**             | `~/.claude/cache/permissions-check.json` — weekly drift check timestamps per repo      |
| **managed paths**     | Repos under the own/work prefixes of `config.toml` — the only paths the tool acts on   |
