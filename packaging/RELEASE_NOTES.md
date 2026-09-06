# Yaver 0.2.1

Patch over 0.2.0: the Windows exe no longer crashes when `.env` is present
(console encoding). Double-click / no-args starts the daemon + dashboard.

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

### Source code

GitHub attaches **Source code (zip)** and **Source code (tar.gz)** for this tag.

## Highlights

- Frozen `yaver` / `yaver.exe` from CI
- OpenCoderman **derman-build** / **derman-plan**
- Codex worker, ops dashboard, Jira webhook intake
- GitLab MR mention jobs; clone cleanup when an MR closes
- Serve loop: compact wait, one unattended nudge, no empty MRs after ERROR

## Config (secrets stay out of the binary)

Copy `.env.example` next to the executable. Full comments live in that file. Minimum:

```env
JIRA_HOST=https://your-jira.example.com
JIRA_API_TOKEN=your-api-token-here
JIRA_BOARD_ID=1
```

Durable data: `YAVER_DATA_DIR` (`C:\vd\yaver` / `/vd/yaver`). Temp clones: `TEMP_DIR_BASE` (`C:\vd\t` / `/vd/t`).
