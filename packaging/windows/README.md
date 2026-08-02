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
2. Extract once (you should see `install.bat` and `start.bat` at the top level).
3. Install a supported Python 3.x x64 (see `vendor\SUPPORTED_PYTHON.txt`).
4. Run **`install.bat`**:
   - Creates `.venv` and installs Python deps from **`vendor\python-wheels`** (offline)
   - Extracts OpenCode into **`%USERPROFILE%\.opencode`**
   - Ensures **`web\dist`** (prebuilt ops dashboard SPA) is present
5. Edit **`.env`** (Jira / GitLab).
6. Run **`start.bat`**:
   - Stops any previous instance (port 8080 / old `python -m src.daemon`)
   - Starts the daemon (poller + API + **ops dashboard UI** on http://127.0.0.1:8080)
7. Optional OpenCode TUI: **`start-opencode.bat`** (never from your user home folder).

### Frontend + backend model (offline)

| Piece | How it is shipped / started |
|-------|-----------------------------|
| **Backend** | Python daemon (`python -m src.daemon`) via `start.bat` |
| **Frontend** | React SPA built in CI (`npm run build` in `web/`), copied to **`web\dist`**, served by FastAPI on the **same port** as the API |
| **Node at runtime** | **Not required** on the user machine (only CI builds the SPA) |

**Do not split into separate start-frontend / start-backend scripts for the offline zip.**  
A second “frontend” process would be Vite (port 5173) and needs Node + `web/node_modules`, which we deliberately do **not** ship. The product model is:

```text
start.bat  →  one process on http://127.0.0.1:8080
              ├── REST + WebSocket  (/api/*, /ws)
              └── SPA UI            (web\dist → /, /assets/*)
```

If the browser shows **JSON** at `/` (“Dashboard API is running…”), `web\dist` is missing from the install folder — re-download a CI package that includes the SPA build (or run `npm run build` in `web/` on a machine with Node).

Do **not** ship `web\node_modules` in the zip (path-length bomb). Only `web\dist`.

**CI note:** full `e2e-smoke.ps1` (deep-path install.bat) is **not** run on every push (too slow). Build still asserts payload layout (SPA + launchers + vendor).

OpenCode is installed **only** under **`%USERPROFILE%\.opencode`** (binary, config, plugin).
Config is mirrored to **`%USERPROFILE%\.config\opencode\`** for OpenCode global discovery.

Do **not** expect a second install at `C:\vd\opencode` (that was a short-lived workaround).
Advanced override: set `VD_OPENCODE_ROOT` before running `install.bat`.

## Design notes (Windows pain points)

| Problem | Fix |
|---------|-----|
| Path too long / slow extract of `node_modules` | Outer zip only has **`vendor/opencode-home.zip`** (one file). `install.bat` extracts it with long-path-aware tools into `%USERPROFILE%\.opencode` |
| Python version lock-in | Offline wheels downloaded for **3.10, 3.11, 3.12, 3.13** (`PYTHON_WHEEL_VERSIONS`); runtime requires **≥ 3.10** |
| `opencode.json` became `[OK] config ...` | **cmd.exe** treats unescaped `>` in `echo ... -> file` as redirect — installer never uses bare `->` in echo lines |
| Multiple `opencode` on PATH | Installer adds only `%USERPROFILE%\.opencode\bin` and drops legacy `C:\vd\opencode\bin` from user PATH |
| Dirty re-install | `install.bat` wipes prior `%USERPROFILE%\.opencode`, legacy `C:\vd\opencode`, and bad `.config\opencode\opencode.json` before extract |
| Black/blank TUI / default agents | OpenCode Bun-installs plugins into `~/.cache/opencode`; installer **full-copies** the complete `oh-my-opencode` tree (agents + skill `.md`), pins version, seeds `node_modules` + `packages` + `.config` |

## Files

| Path | Role |
|------|------|
| `versions.env` | Pinned OpenCode / oh-my-opencode / glab / Python wheel set / Node |
| `package.json` | Template for `%USERPROFILE%\.opencode\package.json` |
| `opencode.json` | Registers `oh-my-opencode` plugin |
| `oh-my-opencode.json` | Default plugin config stub |
| `build-dist.ps1` | Fetches pinned artifacts, **builds `web/` SPA**, packs the zip |
| `start.bat` | User launcher: kill old processes → start daemon + dashboard |
| `Stop-VdProcesses.ps1` | Helper used by `start.bat` to free ports / kill old daemons |
| `e2e-smoke.ps1` | CI: deep-path install + assert SPA + launchers + OpenCode |
| `../../.github/workflows/windows-dist.yml` | Runs the packager on `windows-latest` |

## Bumping versions

1. Edit `versions.env` (and the version inside `package.json` if you change oh-my-opencode).
2. Push to `develop` / `main`, or run the **Windows Distribution** workflow manually.
3. Download the new artifact and smoke-test `install.bat` on a clean Windows machine.

## Local pack (Windows)

```powershell
# Requires Python 3.12 + Node 20 on PATH
.\packaging\windows\build-dist.ps1 -OutDir .\dist
```

Output: `dist\virtual_developer-windows-x64.zip` (or the name you pass via `-DistName`).
