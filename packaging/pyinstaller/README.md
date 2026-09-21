# Standalone Yaver executables

PyInstaller **onedir** freeze of the Yaver CLI + daemon. This is an
**additive** packaging track — it does **not** replace the Windows/Linux
offline zips (`install-dashboard` + `install-backends`).

| Platform | Binary | CI workflow |
|----------|--------|-------------|
| Windows x64 | `yaver.exe` | `.github/workflows/executables.yml` |
| Ubuntu 18.04 | `yaver` | same workflow, Docker `ubuntu:18.04` (glibc 2.27) |
| Ubuntu 20.04 | `yaver` | same workflow, Docker `ubuntu:20.04` (glibc 2.31) |
| Ubuntu 22.04 | `yaver` | same workflow, Docker `ubuntu:22.04` (glibc 2.35) |
| Ubuntu 24.04 | `yaver` | same workflow, Docker `ubuntu:24.04` (glibc 2.39) |

OpenCode and Codex are **not** inside this binary. Install them separately.

## Config

| File | Role |
|------|------|
| `versions.env` | Pinned Python / Node / PyInstaller versions |
| `yaver.spec` | What gets frozen (hidden imports, datas, onedir) |
| `entrypoint.py` | Process entry (`cli.py` commands) |
| `runtime_hook.py` | `chdir` to the exe folder so `.env` is found |
| `build.py` | Local + CI build driver |
| `START_HERE.txt` | Shipped next to the binary |

Operator config is still **`.env` next to the executable**. Secrets are
never baked into the binary. Copy `.env.example` → `.env` and edit.

Leave `YAVER_BASE_DIR` unset for the per-user default:
`%LOCALAPPDATA%\Yaver` on Windows, `~/.local/share/yaver` on Linux.
Data is `{base}/yaver` and clones are `{base}/t`. Plans are
`{base}/yaver/plans/`.

## User flow

1. Download the Actions artifact (`yaver-windows-x64-*` or `yaver-linux-x64-ubuntu-22.04-*` — pick the Ubuntu that matches the host).
2. Extract. You should see `yaver.exe` / `yaver`, `_internal/`, `.env.example`, `START_HERE.txt`, `opencoderman/` (`agents/` + `skills/` only), and one copy script (`install-opencode-agents.bat` on Windows, `install-opencode-agents.sh` on Linux).
3. Copy `.env.example` to `.env` and set Jira (and GitLab if you need MRs).
4. If OpenCode is already installed, run that copy script to put `opencoderman/agents` and `opencoderman/skills` into the OpenCode home.
5. Run `yaver start` (Windows: `yaver.exe start`).
6. Open http://127.0.0.1:8080

```text
yaver --help
yaver --version
yaver start
yaver process KEY-123
```

## Local build

```bash
# SPA first
cd web && npm ci && npm run build && cd ..

# Freeze (from repo root, Python 3.12)
python -m pip install -r requirements.txt
python -m pip install "pyinstaller==$(python -c "import pathlib; print([l.split('=',1)[1].strip() for l in pathlib.Path('packaging/pyinstaller/versions.env').read_text().splitlines() if l.startswith('PYINSTALLER_VERSION=')][0])")"
python packaging/pyinstaller/build.py --clean
```

Output: `dist/stage/yaver-<platform>-<version>/` plus a zip (and `.tar.gz` on Linux).

## Do / don’t

**Do**

- Keep `PYINSTALLER_MODE=onedir`.
- Bundle `web/dist`, `agent/`, `VERSION`, `.env.example`, `opencoderman.pin`, `opencoderman/agents` + `opencoderman/skills` only, and one copy script (`install-opencode-agents.bat` or `.sh`).
- Resolve `.env` from the folder next to the exe (`install_root`), not `_MEIPASS`.
- Re-run **Standalone Executables** after changing `yaver.spec` or `versions.env`.
- Freeze each Linux target **inside** `ubuntu:18.04` / `20.04` / `22.04` /
  `24.04` via `freeze-in-ubuntu.sh`. PyInstaller ships that image's libs.
  A 24.04 freeze needs `GLIBC_2.38` and will not start on 22.04 or older.

**Don’t**

- Replace `windows-dist.yml` / `linux-dist.yml` with this freeze.
- Ship secrets inside the spec or binary.
- Switch to onefile without a product decision (slow start, temp extract).
- Expect OpenCode/Codex to appear inside `_internal`.
- Freeze Linux on the GitHub runner (`ubuntu-latest` / 24.04) and ship one
  generic `yaver-linux-x64`. That is how 0.9.14 failed on older glibc.
