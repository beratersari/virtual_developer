# Changelog

All notable changes to Yaver are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [SemVer](https://semver.org/) from the repo root `VERSION` file.
GitHub Releases are cut from tags `vMAJOR.MINOR.PATCH`.

## [0.2.1] — 2026-09-06

### Fixed

- Frozen `yaver.exe` crashed on startup as soon as a `.env` was present: a debug log used `≈`, which Windows cp1252 cannot encode. The process died before the ops dashboard bound :8080. Logger now never raises on console encoding, and a no-argument / double-click launch starts the daemon.

[0.2.1]: https://github.com/beratersari/virtual_developer/releases/tag/v0.2.1

## [0.2.0] — 2026-09-06

First tagged product release. Builds from `develop` (`v0.2.0`).

### Added

- Standalone **Windows** (`yaver.exe`) and **Linux** (`yaver`) executables via PyInstaller onedir, built in CI (`.github/workflows/executables.yml`). Operator config is `.env` next to the binary (copy from `.env.example`).
- OpenCoderman submodule: unattended **derman-build** / **derman-plan** agents (not stock OpenCode `build` / `plan`).
- Codex worker (`AGENT_BACKEND=codex`) with the same job contract as OpenCode.
- Ops dashboard (FastAPI + React): jobs, poll monitor, settings, storage, live chat, schedules, issue report zip.
- Jira **webhook** intake (`JIRA_INTAKE_MODE=webhook`) in addition to board poll.
- GitLab MR comment mentions as queued builds; delete the temp clone when an MR is merged or closed.
- Job search (issue key, title, description), storage MR link/status, schedule param picker.
- Assign handled Jira issues to the PAT user.

### Fixed

- Do not open an empty MR after an agent ERROR; keep the job in ERROR.
- Flatten Jira Cloud ADF so `{params}` parse on Cloud issues.
- Do not retry unregistered OpenCode agents.
- Push existing commits after an agent-session error; recover skipped schedules and stale Continue todos.
- OpenCode serve: compact wait, one unattended nudge, last-turn-only clarifying questions, abort compact-loop then Continue the same session.
- Cancel kills agent children immediately; unattended PAT clone; GitLab PAT used for clone/push.

### Packaging

- Windows and Linux offline zips still ship `install-dashboard` + `install-backends` + `install-codex` (Python wheels, OpenCode, Codex). Standalone executables do **not** replace those zips.
- Offline zips include `.env.example`.
- Standalone zip includes `.env.example`, `versions.env`, `START_HERE.txt`, and `VERSION`.

### Downloads (this release)

| Asset | What it is |
|-------|------------|
| `yaver-windows-x64-0.2.0.zip` | Frozen `yaver.exe` + `_internal/` + config templates |
| `yaver-linux-x64-0.2.0.zip` / `.tar.gz` | Frozen `yaver` + `_internal/` + config templates |
| `virtual_developer-windows-x64-0.2.0.zip` | Full Windows offline installer (needs host Python) |
| `virtual_developer-linux-x64-0.2.0.zip` / `.tar.gz` | Full Linux offline installer (needs host Python) |
| Source code (zip / tar.gz) | Git tree at tag `v0.2.0` (added by GitHub) |

[0.2.0]: https://github.com/beratersari/virtual_developer/releases/tag/v0.2.0
