"""The CLI offline zip installs OpenCode, Codex, and Claude without agents."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "packaging" / "windows" / "cli-offline"
BATS = {
    "opencode": CLI / "install-opencode.bat",
    "codex": CLI / "install-codex.bat",
    "claude": CLI / "install-claude.bat",
}


def test_each_worker_has_its_own_bat_and_host_config():
    opencode = (CLI / "opencode.json").read_text(encoding="utf-8")
    codex = (CLI / "config.toml").read_text(encoding="utf-8")
    claude = (CLI / "settings.json").read_text(encoding="utf-8")
    assert opencode.count("YOUR_HOST") == 1
    assert '"apiKey": "{env:AI_API_KEY}"' in opencode
    assert "YOUR_TOKEN" not in opencode
    assert "YOUR_HOST" in codex
    assert 'env_key = "AI_API_KEY"' in codex
    assert "CUSTOM_HOST_TOKEN" not in codex
    assert "ANTHROPIC_BASE_URL" in claude
    assert "YOUR_HOST" in claude
    assert "apiKeyHelper" not in claude
    assert "AI_API_KEY" not in claude
    assert "ANTHROPIC_AUTH_TOKEN" not in claude
    assert "YOUR_TOKEN" not in claude
    assert not (CLI / "ai-api-key.cmd").exists()
    assert not (ROOT / "packaging" / "linux" / "cli-offline" / "ai-api-key").exists()
    claude_bat = (CLI / "install-claude.bat").read_text(encoding="utf-8")
    assert "ai-api-key" not in claude_bat
    assert "AI_API_KEY" not in claude_bat
    claude_sh = (
        ROOT / "packaging" / "linux" / "cli-offline" / "install-claude.sh"
    ).read_text(encoding="utf-8")
    assert "AI_API_KEY" not in claude_sh
    codex_bat = (CLI / "install-codex.bat").read_text(encoding="utf-8")
    assert "AI_API_KEY" in codex_bat
    assert "CUSTOM_HOST_TOKEN" not in codex_bat
    for name, bat in BATS.items():
        text = bat.read_text(encoding="utf-8")
        assert f"{name}\\" in text
        assert "copy /Y" in text
        assert "Backup-CliBinary.ps1" in text
        assert "curl" not in text.lower()
        assert "github.com" not in text.lower()
        assert "agents\\" not in text.lower()
        assert "skills\\" not in text.lower()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("echo ") and " -> " in stripped:
                raise AssertionError(f"cmd.exe redirect landmine: {stripped}")


def test_windows_cli_zips_are_packed_before_the_download_cache_is_removed():
    text = (ROOT / "packaging" / "windows" / "build-dist.ps1").read_text(encoding="utf-8")
    pack = text.index('New-CliZip -Tool "opencode"')
    drop = text.index("Remove-Item -LiteralPath $dl")
    assert pack < drop


def test_build_dist_ships_a_separate_cli_zip_without_agents():
    text = (ROOT / "packaging" / "windows" / "build-dist.ps1").read_text(encoding="utf-8")
    assert "yaver-clis-" not in text
    assert 'New-CliZip -Tool "opencode"' in text
    assert 'New-CliZip -Tool "claude"' in text
    assert 'New-CliZip -Tool "codex"' in text
    assert '$zipBase = "yaver-$Tool-$OsToken-$Version"' in text
    assert "cli-offline" in text
    assert "CLAUDE_CODE_VERSION" in text
    assert "downloads.claude.ai/claude-code-releases/" in text
    assert 'Filter "agents"' in text
    assert 'Join-Path $toolStage "VERSION"' in text
    versions = (ROOT / "packaging" / "windows" / "versions.env").read_text(encoding="utf-8")
    assert "OPENCODE_VERSION=1.18.10" in versions
    assert "CODEX_VERSION=0.149.0" in versions
    assert "CLAUDE_CODE_VERSION=2.1.280" in versions
    assert 'Filter "codex.exe"' in text
    assert 'Filter "codex*.exe"' not in text
    assert 'OpenCode must be 1.18.10' in text
    assert 'Codex must be 0.149.0' in text
    assert 'Claude Code must be 2.1.280' in text
    linux = (ROOT / "packaging" / "linux" / "build-dist.sh").read_text(encoding="utf-8")
    assert 'OpenCode must be 1.18.10' in linux
    assert 'Codex must be 0.149.0' in linux
    assert "Claude Code must be 2.1.280" in linux
    assert "ClaudeCode=2.1.280" in linux
    assert "yaver-clis-" not in linux
    assert "write_cli_zip opencode" in linux
    assert "write_cli_zip claude" in linux
    assert "write_cli_zip codex" in linux
    assert "ai-api-key" not in text
    assert "ai-api-key" not in linux
    assert 'base="yaver-${tool}-linux-x64-${version}"' in linux
    assert '>"$stage/VERSION"' in linux
    assert "linux-x64/claude" in linux
    assert "cli-offline" in linux
    linux_cli = ROOT / "packaging" / "linux" / "cli-offline"
    for name in ("install-opencode.sh", "install-codex.sh", "install-claude.sh"):
        script = (linux_cli / name).read_text(encoding="utf-8")
        assert "vd_install_binary" in script
        assert "agents/" not in script
    helper = (linux_cli / "lib.sh").read_text(encoding="utf-8")
    assert "date +%Y%m%d" in helper
    assert (ROOT / "packaging" / "windows" / "Backup-CliBinary.ps1").is_file()
    workflow = (ROOT / ".github" / "workflows" / "linux-dist.yml").read_text(encoding="utf-8")
    assert "yaver-clis-" not in workflow
    for name in ("opencode", "claude", "codex"):
        assert f"yaver-{name}-linux-x64-" in workflow
    windows = (ROOT / ".github" / "workflows" / "windows-dist.yml").read_text(encoding="utf-8")
    assert "yaver-clis-" not in windows
    for name in ("opencode", "claude", "codex"):
        assert f"yaver-{name}-windows-x64-" in windows
