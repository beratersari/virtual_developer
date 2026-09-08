# Yaver 0.3.0

Plan → build is label-driven (`plan_ready` / `plan_execute` /
`plan_refactor`). Plan and build keep **separate** OpenCode sessions
for the same repo + source + target. Plans are Jira comments only
(not the description) and live under `{YAVER_DATA_DIR}/plans/`.

A new `Mode: build` ticket implements the existing plan when one
exists for that repo/branches. The ops dashboard Sessions page
lists plan vs build maps separately. Live WebSocket ticks no longer
rescan every job on each poll.

OpenCoderman on this release: `derman-plan` is git-read-only and
ends with `PLAN_DONE`; `derman-build` cannot push.

The exact OpenCoderman submodule commit is in `opencoderman.pin` and
`opencoderman-<sha>.zip` on this release.

Changelog: see `CHANGELOG.md` in the source tree.

## What to download

### Standalone executables (no host Python)

| File | Platform |
|------|----------|
| `yaver-windows-x64-*.zip` | Windows x64 |
| `yaver-linux-x64-*.zip` or `.tar.gz` | Linux x64 |

Each archive is an **onedir** folder:

- `yaver.exe` / `yaver` — CLI + daemon (same commands as `python cli.py`)
- `_internal/` — bundled Python runtime, SPA (`web/dist`), prompts
- `opencoderman/` — OpenCoderman tree (agents, skills, install.py)
- `install-opencode-agents.bat` / `.sh` — detect the OpenCode home and copy `derman-build`, `derman-plan`, and `skills/`
- `opencode_configs/` — same agents + skills copy
- **Config templates (edit these, do not commit secrets):**
  - `.env.example` → copy to `.env` and set Jira / GitLab
  - `versions.env` — pinned freeze versions (Python / Node / PyInstaller)
  - `START_HERE.txt` — short operator steps
  - `VERSION`

```text
copy .env.example .env     # Windows
cp .env.example .env       # Linux
# edit .env  (JIRA_HOST, JIRA_API_TOKEN, JIRA_BOARD_ID, …)
yaver.exe start            # Windows
./yaver start              # Linux
# open http://127.0.0.1:8080
```

OpenCode and Codex are **not** inside these binaries. Install them from the full offline zip (`install-backends` / `install-codex`) or on your own.

### Full offline installers (Python + OpenCode + Codex vendor)

| File | Platform |
|------|----------|
| `virtual_developer-windows-x64-*.zip` | Windows |
| `virtual_developer-linux-x64-*.zip` / `.tar.gz` | Linux |

Extract, run `install-dashboard` then `install-backends` (and `install-codex` if needed), edit `.env`, start with `start-backend` / `start.bat`.

### OpenCoderman snapshot

Each release also attaches `opencoderman-<sha>.zip` — the **exact**
submodule tree this tag was built with (commit is in `opencoderman.pin`
inside the exe zip and the offline installers). Later submodule bumps
do not change that file.

### Source code

GitHub attaches **Source code (zip)** and **Source code (tar.gz)** for this tag.

## Highlights

- Label-driven plan handoff: `plan_ready` → `plan_execute` / `plan_refactor`
- Separate OpenCode plan vs build sessions; Sessions page split
- Plans as Jira comments only; files under `{YAVER_DATA_DIR}/plans/`
- `Mode: build` implements the existing plan when one exists
- Slim dashboard live ticks (no full job rescan every poll)
- OpenCoderman **derman-build** / **derman-plan**
- Frozen `yaver` / `yaver.exe` from CI; offline Windows/Linux zips

## Config (secrets stay out of the binary)

Copy `.env.example` next to the executable. Full comments live in that file. Minimum:

```env
JIRA_HOST=https://your-jira.example.com
JIRA_API_TOKEN=your-api-token-here
JIRA_BOARD_ID=1
```

Durable data: `YAVER_DATA_DIR` (`C:\vd\yaver` / `/vd/yaver`). Temp clones: `TEMP_DIR_BASE` (`C:\vd\t` / `/vd/t`). Plans: `{YAVER_DATA_DIR}/plans/`.
