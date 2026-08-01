# Windows offline distribution

This folder drives the **Windows-only** offline installer shipped as a zip from CI.

## User flow

1. Download `virtual_developer-windows-x64-*.zip` (Actions artifact or GitHub Release).
2. Extract.
3. Run `install.bat`.

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
| Black/blank TUI | OpenCode fetches npm plugins into `~/.cache/opencode` at start; installer **pins** `oh-my-opencode@VERSION`, seeds that cache offline, sets `autoupdate: false` |

## Files

| Path | Role |
|------|------|
| `versions.env` | Pinned OpenCode / oh-my-opencode / glab / Python wheel set / Node |
| `package.json` | Template for `%USERPROFILE%\.opencode\package.json` |
| `opencode.json` | Registers `oh-my-opencode` plugin |
| `oh-my-opencode.json` | Default plugin config stub |
| `build-dist.ps1` | Fetches pinned artifacts from the web and builds the zip |
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
