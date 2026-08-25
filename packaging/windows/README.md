# Windows offline distribution

This folder drives the **Windows-only** offline installer shipped as a zip from CI.

## Versioning (SemVer)

| Source of truth | File |
|-----------------|------|
| Product base version | repo root `VERSION` (`MAJOR.MINOR.PATCH`) |

| Trigger | Dist name example |
|---------|-------------------|
| Tag `v0.2.0` | `virtual_developer-windows-x64-0.2.0` (+ GitHub Release) |
| Push `develop` | `virtual_developer-windows-x64-0.2.0-dev.20260801.42.gb99d2d9` |
| Push `main` | `virtual_developer-windows-x64-0.2.0.gb99d2d9` |
| Manual dispatch | same as branch + optional suffix |

Resolver: `resolve-version.ps1` (used by `.github/workflows/windows-dist.yml`).

Bump product releases by editing `VERSION`, merging to `develop`/`main`, and tagging `vX.Y.Z` for a formal Release zip.

## User flow

1. Download `virtual_developer-windows-x64-*.zip` (Actions artifact or GitHub Release).
   The zip already includes the prebuilt ops dashboard SPA (`web\dist`).
2. Extract once (you should see `install-dashboard.bat`, `install-backends.bat`, `install-codex.bat`, and `start.bat` at the top level).
3. Install a supported Python 3.x x64 (see `vendor\SUPPORTED_PYTHON.txt`).
4. Install (run dashboard + backends for a full offline box):
   - **`install-dashboard.bat`** — **Python + ops dashboard** (no agent workers):
     - Creates `.venv` + deps from **`vendor\python-wheels`**, start scripts, `.env`, `cli.py init`
     - Ensures **`web\dist`** (prebuilt ops dashboard SPA) is present
   - **`install-dashboard-system-python.bat`** — same as dashboard install, **no `.venv`**:
     - Uses `python` already on PATH and `pip install -r requirements.txt` into that interpreter
     - `start-backend.bat` / `start-frontend.bat` fall back to system `python` when `.venv` is missing
   - **`install-backends.bat`** — **OpenCode** (no Python / dashboard); also installs Codex when run with no args:
     - OpenCode to **`%USERPROFILE%\.opencode`**
     - Optional: `install-backends.bat opencode` (OpenCode only)
   - **`install-codex.bat`** — **Codex CLI only**:
     - Extracts **`vendor\codex-package-x86_64-pc-windows-msvc.tar.gz`** with **`tar.exe`**
       (or downloads that asset from GitHub when vendor is missing)
     - Codex to **`%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe`**
     - Dummy **`%USERPROFILE%\.codex\config.toml`** if that file is not already there
     - Does not touch OpenCode or Python
   - **`install-opencode-online.bat`** — **OpenCode only, online** (needs network):
     - **Requires** portable **`vendor\node\node.exe`** + `npm.cmd` (no system Node)
     - Edit **`vendor\npm-online.npmrc`** (or `packaging\windows\npm-online.npmrc`) → set `registry=` to your npm mirror
     - Optional: **`vendor\online-sources.env`** for `OPENCODE_ZIP_URL` / `NPM_REGISTRY` pointing at your HTTP file server
     - **Offline workers still use `install-backends.bat` + `vendor\opencode-home.zip`**
5. Edit **`.env`** (Jira / GitLab).
6. Start (pick one):
   - **`start-backend.bat`** — ensures OpenCode serve on **:4096**, then daemon on **http://0.0.0.0:8080/** (API + SPA)
   - **`start-frontend.bat`** — separate UI on **http://0.0.0.0:5173/** (proxies `/api` + `/ws` to backend; **no Node/Vite**)
   - **`start.bat`** — both (backend first, then frontend)
7. Optional OpenCode TUI: **`start-opencode.bat`** (after `install-backends.bat` or `install-opencode-online.bat`; never from your user home folder).

### Frontend + backend model (offline)

| Launcher | Port | Role |
|----------|------|------|
| **start-backend.bat** | **8080** (+ **4096** serve) | Ensures OpenCode serve, then daemon: poller, jobs, REST, WebSocket, and SPA from `web\dist` |
| **start-frontend.bat** | **5173** | SPA only + reverse proxy to backend (so you can use :5173 without Node) |
| **start.bat** | both | Calls backend, then frontend |

```text
start-backend.bat  →  http://0.0.0.0:8080/   (open http://127.0.0.1:8080/)
start-frontend.bat →  http://0.0.0.0:5173/   (open http://127.0.0.1:5173/)
                         └── proxies /api and /ws → http://127.0.0.1:8080
```

- **Node is not required at runtime.** Frontend is a small Python server (`serve_frontend.py`) over prebuilt `web\dist`.
- Default bind is **0.0.0.0** (LAN). Set `DASHBOARD_HOST=127.0.0.1` in `.env` to lock down.
- If `/` on :8080 returns **JSON**, `web\dist` is missing — use a CI zip that includes the SPA.

Do **not** ship `web\node_modules` in the zip. Only `web\dist`.

**CI note:** full `e2e-smoke.ps1` is not run on every push (too slow). Build asserts payload layout.

OpenCode is installed **only** under **`%USERPROFILE%\.opencode`** (binary, config, plugin).
Config is mirrored to **`%USERPROFILE%\.config\opencode\`** for OpenCode global discovery.

Do **not** expect a second install at `C:\vd\opencode` (that was a short-lived workaround).
Advanced override: set `VD_OPENCODE_ROOT` before running `install-backends.bat`.

## Design notes (Windows pain points)

| Problem | Fix |
|---------|-----|
| Path too long / slow extract of `node_modules` | Outer zip only has **`vendor/opencode-home.zip`** (one file). `install-backends.bat` extracts it with long-path-aware tools into `%USERPROFILE%\.opencode` |
| Python version lock-in | Offline wheels downloaded for **3.10, 3.11, 3.12, 3.13** (`PYTHON_WHEEL_VERSIONS`); runtime requires **≥ 3.10** |
| `opencode.json` became `[OK] config ...` | **cmd.exe** treats unescaped `>` in `echo ... -> file` as redirect — installer never uses bare `->` in echo lines |
| Multiple `opencode` on PATH | Installer adds only `%USERPROFILE%\.opencode\bin` and drops legacy `C:\vd\opencode\bin` from user PATH |
| Dirty re-install | `install-backends.bat` wipes prior `%USERPROFILE%\.opencode`, legacy `C:\vd\opencode`, and bad `.config\opencode\opencode.json` before extract |
| Black/blank TUI / default agents | OpenCode Bun-installs plugins into `~/.cache/opencode`; installer **full-copies** the complete `oh-my-opencode` tree (agents + skill `.md`), pins version, seeds `node_modules` + `packages` + `.config` |

## Files

| Path | Role |
|------|------|
| `versions.env` | Pinned OpenCode / Codex / oh-my-opencode / glab / Python wheel set / Node |
| `package.json` | Template for `%USERPROFILE%\.opencode\package.json` |
| `opencode.json` | Stock OpenCode config (`plugin: []`, built-in build/plan) |
| `oh-my-opencode.json` | Default plugin config stub |
| `Install-Backends.ps1` | Offline OpenCode and/or Codex (called by root `install-backends.bat` / `install-codex.bat`) |
| `Install-OpencodeOnline.ps1` | Online OpenCode CLI install (called by root bat; not used by offline `install-backends.bat`) |
| `npm-online.npmrc` | Editable npm `registry=` for online install only |
| `online-sources.env` | Optional `OPENCODE_ZIP_URL` / `NPM_REGISTRY` mirrors |
| `build-dist.ps1` | Fetches pinned artifacts, **builds `web/` SPA**, packs the zip |
| `start.bat` | User launcher: kill old processes → start daemon + dashboard |
| `Stop-VdProcesses.ps1` | Helper used by `start.bat` to free ports / kill old daemons |
| `e2e-smoke.ps1` | CI: deep-path install + assert SPA + launchers + OpenCode |
| `../../.github/workflows/windows-dist.yml` | Runs the packager on `windows-latest` |

Payload also ships **`vendor/node/`** (official Node win-x64 tree: `node.exe`, `npm.cmd`, npm deps) for online OpenCode installs without a system Node.

## Bumping versions

1. Edit `versions.env` (and the version inside `package.json` if you change oh-my-opencode).
2. Push to `develop` / `main`, or run the **Windows Distribution** workflow manually.
3. Download the new artifact and smoke-test `install-dashboard.bat` + `install-backends.bat` + `install-codex.bat` on a clean Windows machine.

## Local pack (Windows)

```powershell
# Requires Python 3.12 + Node 20 on PATH
.\packaging\windows\build-dist.ps1 -OutDir .\dist
```

Output: `dist\virtual_developer-windows-x64.zip` (or the name you pass via `-DistName`).
