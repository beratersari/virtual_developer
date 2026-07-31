# Windows offline distribution

This folder drives the **Windows-only** offline installer shipped as a zip from CI.

## User flow

1. Download `virtual_developer-windows-x64-*.zip` (Actions artifact or GitHub Release).
2. Extract.
3. Run `install.bat`.

OpenCode is installed under **`%USERPROFILE%\.opencode`** (binary, config, plugin).

## Files

| Path | Role |
|------|------|
| `versions.env` | Pinned OpenCode / oh-my-opencode / glab / Python / Node versions |
| `package.json` | Template for `%USERPROFILE%\.opencode\package.json` |
| `opencode.json` | Registers `oh-my-opencode` plugin |
| `oh-my-opencode.json` | Default plugin config stub |
| `build-dist.ps1` | Fetches pinned artifacts from the web and builds the zip |
| `../.github/workflows/windows-dist.yml` | Runs the packager on `windows-latest` |

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
