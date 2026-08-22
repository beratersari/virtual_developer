"""Discover OpenCode models for settings UI and CLI.

Sources (merged, de-duplicated):
1. ``opencode models`` CLI (what the installed CLI can actually use)
2. ``opencode.json`` / ``opencode.jsonc`` — default ``model`` plus custom
   ``provider.<id>.models`` (local hosts / private registries)
3. Current ``settings.default_model`` so the active choice always appears
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.config import settings
from src.logger import logger

# Brief cache so dashboard polls do not spawn opencode every second
_CACHE_TTL_SEC = 45.0
_cache_lock = threading.Lock()
_cache_at: float = 0.0
_cache_items: List["ModelInfo"] = []
_cache_error: Optional[str] = None
_cache_config_path: Optional[str] = None
_cache_config_model: Optional[str] = None


@dataclass(frozen=True)
class ModelInfo:
    """One selectable model id (provider/model)."""

    id: str
    name: str = ""
    provider: str = ""
    source: str = "cli"  # cli | config | config_default | settings

    def label(self) -> str:
        if self.name and self.name.lower() != self.id.lower():
            return f"{self.id} — {self.name}"
        return self.id


def _split_provider_model(model_id: str) -> Tuple[str, str]:
    mid = (model_id or "").strip()
    if "/" in mid:
        prov, rest = mid.split("/", 1)
        return prov, rest
    return "", mid


def _strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments from JSONC (naive but adequate for config)."""
    # Block comments
    out = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # Line comments (not inside strings — good enough for our configs)
    lines: List[str] = []
    for line in out.splitlines():
        in_str = False
        esc = False
        cut = len(line)
        i = 0
        while i < len(line):
            ch = line[i]
            if esc:
                esc = False
            elif ch == "\\" and in_str:
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                cut = i
                break
            i += 1
        lines.append(line[:cut])
    return "\n".join(lines)


def opencode_config_candidates() -> List[Path]:
    """Paths where OpenCode may store user config (first existing wins for default model)."""
    home = Path.home()
    cwd = Path.cwd()
    project_root = Path(settings.project_root) if settings.project_root else cwd
    return [
        project_root / "opencode.json",
        project_root / "opencode.jsonc",
        cwd / "opencode.json",
        cwd / "opencode.jsonc",
        home / ".config" / "opencode" / "opencode.json",
        home / ".config" / "opencode" / "opencode.jsonc",
        home / ".opencode" / "opencode.json",
        home / ".opencode" / "opencode.jsonc",
    ]


def load_opencode_config() -> Tuple[Optional[Path], Dict[str, Any]]:
    """Load first readable opencode.json / jsonc. Returns (path, data)."""
    for path in opencode_config_candidates():
        try:
            if not path.is_file():
                continue
            raw = path.read_text(encoding="utf-8")
            if path.suffix.lower() == ".jsonc" or "jsonc" in path.name:
                raw = _strip_jsonc(raw)
            data = json.loads(raw)
            if isinstance(data, dict):
                return path, data
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.debug(f"Skipping opencode config {path}: {e}")
            continue
    return None, {}


def models_from_opencode_config(
    data: Optional[Dict[str, Any]] = None,
) -> Tuple[List[ModelInfo], Optional[str]]:
    """Extract model ids from provider.models and top-level model key."""
    if data is None:
        _, data = load_opencode_config()
    by_id: Dict[str, ModelInfo] = {}
    config_default: Optional[str] = None

    def _put(info: ModelInfo) -> None:
        prev = by_id.get(info.id)
        if prev is None:
            by_id[info.id] = info
            return
        # Prefer explicit display name from provider.models over bare id tail
        name = info.name if info.name and info.name != info.id.split("/")[-1] else prev.name
        if prev.name and prev.name != prev.id.split("/")[-1] and (
            not info.name or info.name == info.id.split("/")[-1]
        ):
            name = prev.name
        # Keep config_default source if either was the default key
        source = info.source
        if prev.source == "config_default" or info.source == "config_default":
            source = "config_default"
        by_id[info.id] = ModelInfo(
            id=info.id,
            name=name or info.name or prev.name,
            provider=info.provider or prev.provider,
            source=source,
        )

    default = data.get("model") if data else None
    if isinstance(default, str) and default.strip():
        config_default = default.strip()
        prov, mid = _split_provider_model(config_default)
        _put(
            ModelInfo(
                id=config_default,
                name=mid or config_default,
                provider=prov,
                source="config_default",
            )
        )

    providers = data.get("provider") if data else None
    if not isinstance(providers, dict):
        # Alternate key used in some configs
        providers = data.get("providers") if data else None
    if isinstance(providers, dict):
        for prov_id, prov_body in providers.items():
            if not isinstance(prov_id, str) or not prov_id.strip():
                continue
            if not isinstance(prov_body, dict):
                continue
            models = prov_body.get("models")
            if not isinstance(models, dict):
                continue
            for model_key, model_body in models.items():
                if not isinstance(model_key, str) or not model_key.strip():
                    continue
                full_id = f"{prov_id.strip()}/{model_key.strip()}"
                name = model_key
                if isinstance(model_body, dict):
                    n = model_body.get("name") or model_body.get("id")
                    if isinstance(n, str) and n.strip():
                        name = n.strip()
                _put(
                    ModelInfo(
                        id=full_id,
                        name=name,
                        provider=prov_id.strip(),
                        source="config",
                    )
                )
    return list(by_id.values()), config_default


def models_from_cli(
    *,
    timeout: float = 12.0,
    opencode_cli: Optional[str] = None,
) -> Tuple[List[ModelInfo], Optional[str]]:
    """Run ``opencode models`` and parse plain id lines (or verbose JSON blocks)."""
    cli = (opencode_cli or settings.opencode_cli or "opencode").strip() or "opencode"
    cmd = cli.split() + ["models"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return [], f"OpenCode CLI not found: {cli}"
    except subprocess.TimeoutExpired:
        return [], f"opencode models timed out after {timeout}s"
    except OSError as e:
        return [], f"Failed to run opencode models: {e}"

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:300]
        return [], err or f"opencode models exit {proc.returncode}"

    items: List[ModelInfo] = []
    stdout = proc.stdout or ""
    # Prefer simple one-id-per-line format (default ``opencode models``)
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("{") or line.startswith("["):
            continue
        # Skip noise / headers
        if " " in line and not line.startswith("opencode/") and "/" not in line.split()[0]:
            # e.g. log lines; still accept "provider/model" tokens
            pass
        token = line.split()[0] if line.split() else line
        if "/" not in token:
            continue
        # Reject obvious non-model lines
        if token.startswith("-") or token.startswith("http"):
            continue
        prov, mid = _split_provider_model(token)
        items.append(
            ModelInfo(id=token, name=mid, provider=prov, source="cli")
        )

    # Verbose mode embeds JSON objects; recover ids if plain list empty
    if not items and '"providerID"' in stdout:
        for m in re.finditer(
            r'"providerID"\s*:\s*"([^"]+)"[^}]*?"id"\s*:\s*"([^"]+)"',
            stdout,
            re.DOTALL,
        ):
            prov, mid = m.group(1), m.group(2)
            full = f"{prov}/{mid}"
            items.append(ModelInfo(id=full, name=mid, provider=prov, source="cli"))
        # Alternate order: id before providerID inside objects
        if not items:
            for block in re.finditer(r"\{[^{}]+\}", stdout):
                try:
                    obj = json.loads(block.group(0))
                except json.JSONDecodeError:
                    continue
                mid = obj.get("id")
                prov = obj.get("providerID") or obj.get("provider")
                if isinstance(mid, str) and isinstance(prov, str) and mid and prov:
                    items.append(
                        ModelInfo(
                            id=f"{prov}/{mid}",
                            name=str(obj.get("name") or mid),
                            provider=prov,
                            source="cli",
                        )
                    )

    return items, None


def _merge_models(*groups: Sequence[ModelInfo]) -> List[ModelInfo]:
    """De-dupe by id; prefer richer name / config source labels."""
    by_id: Dict[str, ModelInfo] = {}
    source_rank = {
        "settings": 0,
        "config_default": 1,
        "config": 2,
        "cli": 3,
    }
    for group in groups:
        for m in group:
            mid = m.id.strip()
            if not mid:
                continue
            prev = by_id.get(mid)
            if prev is None:
                by_id[mid] = m
                continue
            # Keep better name; prefer lower rank source as primary source tag
            name = prev.name
            if (not name or name == prev.id) and m.name and m.name != m.id:
                name = m.name
            elif m.name and len(m.name) > len(name or ""):
                name = m.name
            src = prev.source
            if source_rank.get(m.source, 9) < source_rank.get(prev.source, 9):
                src = m.source
            by_id[mid] = ModelInfo(
                id=mid,
                name=name or mid,
                provider=prev.provider or m.provider,
                source=src,
            )
    # Sort: provider then id
    return sorted(by_id.values(), key=lambda x: (x.provider.lower(), x.id.lower()))


def list_available_models(
    *,
    refresh: bool = False,
    timeout: float = 12.0,
) -> Tuple[List[ModelInfo], Optional[str], Optional[str], Optional[str]]:
    """Return (models, error, config_path, config_default_model).

    Uses a short in-process cache unless ``refresh`` is True.
    """
    global _cache_at, _cache_items, _cache_error, _cache_config_path, _cache_config_model

    now = time.monotonic()
    with _cache_lock:
        if (
            not refresh
            and _cache_items
            and (now - _cache_at) < _CACHE_TTL_SEC
        ):
            return list(_cache_items), _cache_error, _cache_config_path, _cache_config_model

    cfg_path, cfg_data = load_opencode_config()
    cfg_models, cfg_default = models_from_opencode_config(cfg_data)
    cli_models, cli_err = models_from_cli(timeout=timeout)

    settings_models: List[ModelInfo] = []
    current = (settings.default_model or "").strip()
    if current:
        prov, mid = _split_provider_model(current)
        settings_models.append(
            ModelInfo(
                id=current,
                name=mid or current,
                provider=prov,
                source="settings",
            )
        )

    merged = _merge_models(settings_models, cfg_models, cli_models)
    path_str = str(cfg_path) if cfg_path else None

    with _cache_lock:
        _cache_at = time.monotonic()
        _cache_items = list(merged)
        _cache_error = cli_err
        _cache_config_path = path_str
        _cache_config_model = cfg_default

    return merged, cli_err, path_str, cfg_default


DEFAULT_JOB_CONTEXT_LIMIT = 32768
_EXCLUDE_MARKERS = (
    "/opencode.json",
    "opencode.json",
)


def write_workspace_context_limit(
    workdir: Path,
    *,
    model: str,
    context_limit: int,
) -> Optional[Path]:
    """Write a job-local ``opencode.json`` that shrinks the model context.

    OpenCode auto-compact only runs when the session nears the advertised
    window. Zen free models are 190k–1M, so a long Django job never
    compact. A git-excluded project override makes compact fire without
    committing into the customer repo.
    """
    root = Path(workdir)
    if not root.is_dir():
        return None
    try:
        limit = int(context_limit)
    except (TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    mid = (model or "").strip()
    if not mid:
        return None
    provider, model_id = _split_provider_model(mid)
    if not provider or not model_id:
        return None

    _, existing = load_opencode_config()
    plugin = existing.get("plugin") if isinstance(existing, dict) else None
    cfg: Dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "model": mid,
        "provider": {
            provider: {
                "models": {
                    model_id: {
                        "limit": {
                            "context": limit,
                            "output": min(limit, 8192),
                        }
                    }
                }
            }
        },
    }
    if isinstance(plugin, list) and plugin:
        cfg["plugin"] = plugin

    path = root / "opencode.json"
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    _exclude_workspace_opencode_json(root)
    return path


def _exclude_workspace_opencode_json(workdir: Path) -> None:
    """Keep the job-local opencode.json out of the customer git history."""
    info = workdir / ".git" / "info"
    try:
        info.mkdir(parents=True, exist_ok=True)
        exclude = info / "exclude"
        existing = ""
        if exclude.is_file():
            existing = exclude.read_text(encoding="utf-8")
        if any(m in existing for m in _EXCLUDE_MARKERS):
            return
        with exclude.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write("# virtual developer — job-local OpenCode context cap\n")
            fh.write("/opencode.json\n")
    except OSError as e:
        logger.debug(f"Could not git-exclude workspace opencode.json: {e}")


def clear_models_cache() -> None:
    """Drop cached inventory (tests / after config change)."""
    global _cache_at, _cache_items, _cache_error, _cache_config_path, _cache_config_model
    with _cache_lock:
        _cache_at = 0.0
        _cache_items = []
        _cache_error = None
        _cache_config_path = None
        _cache_config_model = None
