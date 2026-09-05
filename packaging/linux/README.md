# Linux install and start scripts

Same product split as the Windows zip: dashboard (Python) is separate from
OpenCode / Codex. Scripts live here and are invoked from the repo root
(`./install-dashboard.sh`, `./start-backend.sh`, …).

## Offline zip (CI)

GitHub Actions workflow **Linux Distribution** (`.github/workflows/linux-dist.yml`)
builds `virtual_developer-linux-x64-*` with:

- `opencoderman/` (install.py, agents, skills, `vendor/bin/linux/opencode`)
- `vendor/opencode-home.zip` + `vendor/bin/opencode` (CLI fallback)
- `vendor/codex-*.tar.gz` + `vendor/bin/codex`
- `vendor/python-wheels` (manylinux)
- prebuilt `web/dist`

Extract the artifact so the install scripts sit next to `vendor/` and `src/`, then:

```bash
./install-dashboard.sh    # .venv from vendor/python-wheels (no network)
./install-backends.sh     # OpenCode via opencoderman/install.py
./install-codex.sh        # Codex from vendor/codex-*.tar.gz
./start-backend.sh
```

Without that zip, OpenCode falls back to `opencoderman/packaging/build_artifact.py --in-place` (official GitHub release). Dashboard still uses PyPI when wheels are missing.

## Online / from git

```bash
./install-dashboard.sh    # .venv, requirements, .env, cli.py init
./install-backends.sh     # OpenCode (+ Codex if no args)
./install-codex.sh        # Codex only
```

`./install.sh` runs dashboard then backends.

OpenCode is configured with `"plugin": []` and `autoupdate: false` (stock
`build` / `plan` agents). Do not install `oh-my-openagent`.

Durable data (not next to the git checkout):

- `/vd/yaver` + `/vd/t` when writable
- otherwise `~/vd/yaver` + `~/vd/t`

## Start

| Script | Port | Process |
|--------|------|---------|
| `./start-backend.sh` | 8080 (+ 4096 serve) | `python -m src.daemon` (foreground; `--daemon` backgrounds) |
| `./start-frontend.sh` | 5173 | `serve_frontend.py` proxy (does **not** kill the daemon) |
| `./start.sh` | both | backend then frontend, both background |
| `./start-opencode.sh` | n/a | OpenCode TUI **in the project folder** |
| `./start-opencode-serve.sh` | 4096 | force-restart `opencode serve` |
| `./stop.sh` | — | daemon + frontend (`--serve` also stops OpenCode serve) |

Open http://127.0.0.1:8080/ (backend also serves `web/dist` when present).

Never run `opencode` from `$HOME` — it treats the profile as the project.
