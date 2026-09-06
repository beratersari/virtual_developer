# Standalone Yaver executables

PyInstaller **onedir** freeze of the Yaver CLI + daemon. This is an
**additive** packaging track — it does **not** replace the Windows/Linux
offline zips (`install-dashboard` + `install-backends`).

| Platform | Binary | CI workflow |
|----------|--------|-------------|
| Windows x64 | `yaver.exe` | `.github/workflows/executables.yml` |
| Linux x64 | `yaver` | same workflow, `ubuntu-latest` |

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

Durable data stays in `YAVER_DATA_DIR` / `TEMP_DIR_BASE` (`C:\vd\yaver`
and `C:\vd\t` on Windows).

## User flow

1. Download the Actions artifact (`yaver-windows-x64-*` or `yaver-linux-x64-*`).
2. Extract. You should see `yaver.exe` / `yaver`, `_internal/`, `.env.example`, `START_HERE.txt`.
3. Copy `.env.example` to `.env` and set Jira (and GitLab if you need MRs).
4. Run `yaver start` (Windows: `yaver.exe start`).
5. Open http://127.0.0.1:8080

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
- Bundle `web/dist`, `agent/`, `VERSION`, `.env.example`, `opencoderman.pin`.
- Resolve `.env` from the folder next to the exe (`install_root`), not `_MEIPASS`.
- Re-run **Standalone Executables** after changing `yaver.spec` or `versions.env`.

**Don’t**

- Replace `windows-dist.yml` / `linux-dist.yml` with this freeze.
- Ship secrets inside the spec or binary.
- Switch to onefile without a product decision (slow start, temp extract).
- Expect OpenCode/Codex to appear inside `_internal`.
