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

### WSL live probe (30+ HTTP requests)

`opencode.integration.json` pins a real OpenCode Zen free model
(`opencode/mimo-v2.5-free`). After the linux `yaver` zip and a Linux
`opencode serve` are up:

```bash
YAVER_BASE=http://127.0.0.1:18081 \
OPENCODE_BASE=http://127.0.0.1:14097 \
python3 packaging/linux/wsl_integration_probe.py
```

The probe fails unless at least 30 distinct requests succeed.

Extract the release zip or tar.gz. The install scripts sit next to `vendor/` and `src/`, with no extra folder around them. Then:

```bash
./install-dashboard.sh    # .venv from vendor/python-wheels (no network)
./install-backends.sh     # OpenCode via opencoderman/install.py
./install-agents.sh  # copy agents/skills into the detected OpenCode home
./install-codex.sh        # Codex from vendor/codex-*.tar.gz
./start-backend.sh
```

Three more release files, `yaver-opencode-linux-x64-*.zip`, `yaver-claude-linux-x64-*.zip`, and `yaver-codex-linux-x64-*.zip`, each hold one CLI and its host config. `install-opencode.sh`, `install-claude.sh`, and `install-codex.sh` find the binary already on PATH, rename it with the date at the end, and put the new binary in that directory. Agents stay in the product zip.

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

Durable data (not next to the git checkout). The default base is
`~/.local/share/yaver` (or `$XDG_DATA_HOME/yaver`):

- `{base}/yaver` — jobs, sessions, plans
- `{base}/t` — temp clones

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

## Same user

Yaver and `opencode serve` have to be the same user. The daemon creates the clone under `~/.local/share/yaver/t`. Serve writes and commits there, and the Transcript tab reads serve's chat database (`~/.local/share/opencode`). Port 8080 does not need root.

A unit with no `User=` runs as root. `sudo nohup opencode serve` is root too, and `sudo` sets `HOME` to `/root`. The login user's serve then cannot write the clone, so the agent commits in `~/.tmp/opencode`. The job can show completed with no merge request, and Transcript stays empty because the dashboard is looking in `/root/.local/share/opencode`.

```ini
[Service]
User=yaver
Group=yaver
```

Start serve as that same user. After a root run, give the trees to that account once, then restart both as the user:

```bash
sudo chown -R yaver:yaver \
  /home/yaver/.local/share/yaver \
  /home/yaver/.local/share/opencode
sudo systemctl restart yaver
```

Replace `yaver` with the account that should own the work. The same note is in the [Linux quick start](../../README.md#linux).
