"""Configuration management for JIRA Virtual Developer."""

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.logger import logger


def bootstrap_dotenv_into_environ(
    *paths: Path,
    override: bool = False,
) -> int:
    """Load KEY=VAL pairs from .env file(s) into ``os.environ``.

    Pydantic Settings only maps *declared* fields (``extra=ignore``), so project
    build tokens (NPM_TOKEN, AWS_*, DOCKER_*, NuGet, etc.) written in ``.env``
    never reached agent children. This bootstrap copies every ``.env`` key into
    the process so ``_agent_subprocess_env`` can inherit the full host env.

    Existing process environment wins unless ``override=True``.
    Returns the number of keys newly applied (approx).
    """
    try:
        from dotenv import dotenv_values
    except ImportError:
        return 0

    candidates: List[Path] = []
    if paths:
        candidates.extend(Path(p) for p in paths if p)
    else:
        # CWD first (how operators run the daemon), then the install folder
        # (repo root, or the directory next to a frozen yaver.exe), then the
        # package root next to src/. Frozen resource trees are read-only.
        candidates.append(Path.cwd() / ".env")
        candidates.append(Path.cwd() / ".env.agent")
        try:
            from src.install_paths import install_root

            root = install_root()
            candidates.append(root / ".env")
            candidates.append(root / ".env.agent")
        except Exception:
            pass
        try:
            pkg_root = Path(__file__).resolve().parent.parent
            candidates.append(pkg_root / ".env")
            candidates.append(pkg_root / ".env.agent")
        except Exception:
            pass

    applied = 0
    seen: set = set()
    for path in candidates:
        try:
            path = path.resolve()
        except OSError:
            continue
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            values = dotenv_values(path)
        except Exception as e:
            logger.warning(f"Could not read dotenv {path}: {e}")
            continue
        for key, value in (values or {}).items():
            if not key or value is None:
                continue
            if not override and key in os.environ:
                continue
            os.environ[key] = str(value)
            applied += 1
        if applied:
            logger.debug(f"Loaded dotenv keys from {path} (applied~{applied})")
    return applied


# Ensure .env tokens exist in os.environ before Settings() and agent children run.
bootstrap_dotenv_into_environ()


def _dotenv_quote(value: str) -> str:
    """Quote a .env value when it contains whitespace or shell-ish characters."""
    raw = "" if value is None else str(value)
    if raw == "":
        return ""
    if re.search(r'[\s#"\'\\$`]', raw):
        escaped = raw.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return raw


def upsert_dotenv_keys(
    updates: Dict[str, str],
    *,
    path: Optional[Path] = None,
) -> int:
    """Insert or replace KEY=value lines in ``.env`` without dropping other keys.

    Used so dashboard-saved Jira host/email/token survive process restart.
    Never logs secret values. Returns the number of keys written.
    """
    if not updates:
        return 0
    dest = path or (Path.cwd() / ".env")
    try:
        dest = dest.resolve()
    except OSError:
        dest = Path(dest)
    if not dest.is_file():
        example = dest.with_name(".env.example")
        try:
            if example.is_file():
                dest.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                dest.write_text("", encoding="utf-8")
        except OSError as e:
            logger.warning(f"Could not create dotenv {dest}: {e}")
            return 0
    try:
        text = dest.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning(f"Could not read dotenv {dest}: {e}")
        return 0
    wanted = {str(k).strip(): ("" if v is None else str(v)) for k, v in updates.items() if str(k).strip()}
    if not wanted:
        return 0
    found: set[str] = set()
    out_lines: List[str] = []
    for line in text.splitlines(keepends=True):
        core = line[:-1] if line.endswith("\n") else line
        if core.endswith("\r"):
            core = core[:-1]
        stripped = core.lstrip()
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", stripped)
        if m and m.group(1) in wanted:
            key = m.group(1)
            out_lines.append(f"{key}={_dotenv_quote(wanted[key])}\n")
            found.add(key)
        else:
            out_lines.append(line if line.endswith("\n") else line + "\n")
    for key, val in wanted.items():
        if key not in found:
            out_lines.append(f"{key}={_dotenv_quote(val)}\n")
    try:
        dest.write_text("".join(out_lines), encoding="utf-8")
    except OSError as e:
        logger.warning(f"Could not write dotenv {dest}: {e}")
        return 0
    for key, val in wanted.items():
        os.environ[key] = val
    logger.info(
        "Updated .env keys: " + ", ".join(sorted(wanted))
    )
    return len(wanted)


# Poller fallback only when neither .env nor runtime set a name.
# Do not use this as the Settings field default (that made the UI look
# like .env.example even after the operator edited JIRA_TRIGGER_USER).
_FALLBACK_TRIGGER_ASSIGNEE_NAMES = [
    "jira ai bot",
    "jira-ai-bot",
    "jiraai",
    "devbot",
]


def format_trigger_users(raw: Any) -> str:
    """Comma-separated trigger names. No leading ``@`` (same for every provider)."""
    return ", ".join(_trigger_user_names(raw))


def _trigger_user_names(raw: Any) -> List[str]:
    """Split a comma list; strip ``@`` and empties. Keep first-seen spelling."""
    out: List[str] = []
    seen: set[str] = set()
    for item in str(raw or "").replace(";", ",").split(","):
        name = item.strip().lstrip("@").strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def _jira_bot_names(raw: Any) -> List[str]:
    """Split a comma list of Jira names; strip @ and empties."""
    return _trigger_user_names(raw)


def compute_stuck_limit_seconds(
    timeout_seconds: float,
    max_retries: int,
    *,
    extra_attempts: int = 0,
) -> float:
    """Wall-clock stuck-watchdog budget for one in-flight issue.

    ``extra_attempts`` covers compact/incomplete continues that are *not*
    generic error retries.
    Formula: ``timeout * (retries + extra + 1) * 1.5``.
    """
    try:
        timeout = float(timeout_seconds or 0)
    except (TypeError, ValueError):
        timeout = 0.0
    try:
        retries = int(max_retries or 0)
    except (TypeError, ValueError):
        retries = 0
    try:
        extra = int(extra_attempts or 0)
    except (TypeError, ValueError):
        extra = 0
    return timeout * (max(0, retries) + max(0, extra) + 1) * 1.5


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    # JIRA Configuration
    # - Prod / on-prem PAT: JIRA_HOST + JIRA_API_TOKEN → Bearer
    # - Cloud (dev): also set JIRA_EMAIL → HTTP Basic (email + API token)
    jira_host: str = Field(default="", description="JIRA instance URL")
    jira_email: str = Field(
        default="",
        description=(
            "Optional Atlassian account email. When set with a token, Jira uses "
            "HTTP Basic (Cloud API tokens). Leave empty for Bearer PAT (prod/on-prem)."
        ),
    )
    jira_api_token: str = Field(
        default="",
        description="JIRA API token (Cloud) or personal access token (on-prem)",
    )
    jira_projects: str = Field(default="PROJ", description="Comma-separated project keys")
    jira_board_id: str = Field(
        default="",
        description="JIRA board id (from URL or GET /rest/agile/1.0/board)",
    )

    # Oh My OpenAgent Configuration
    opencode_cli: str = Field(
        default="opencode",
        description="OpenCode binary for the TUI and `opencode models`. Jobs use serve.",
    )
    opencode_serve_url: str = Field(
        default="http://127.0.0.1:4096",
        description="Base URL for the required opencode serve process",
    )
    project_root: Path = Field(default=Path.cwd(), description="Project root directory")
    sisyphus_plans_dir: Path = Field(default=Path(".sisyphus/plans"))
    default_model: str = Field(
        default="ollama/Qwen3.5-397B-A17B-FP8",
        description="Default model id for OpenCode and Codex jobs (provider/auth stay in each tool's config)",
    )
    agent_backend: str = Field(
        default="opencode",
        description="Unattended worker: opencode | codex",
    )
    codex_cli: str = Field(default="codex", description="Codex CLI binary for AGENT_BACKEND=codex")
    opencode_context_limit: int = Field(
        default=0,
        description=(
            "Optional job-local OpenCode context cap in tokens. "
            "0 = use the model's advertised window (no workspace override)."
        ),
    )
    project_repositories: str = Field(
        default="",
        description=(
            "JSON list of saved git remotes for the dashboard New-issue form. "
            'Example: [{"label":"demo","url":"https://gitlab.com/g/r.git",'
            '"target_branch":"develop"}]'
        ),
    )
    
    # GitLab credentials — repository URL and source branch come from each Jira issue
    # (see src/issue_git_spec.py: Repository + Source + Target; MR source → target)
    #
    # First-class: per-host PATs as JSON. A host with a PAT is allowed.
    #   GITLAB_HOST_PATS={"gitlab.com":"glpat-…","gitlab.internal.com":"glpat-…"}
    # Leftover (only when the JSON map is empty): one GITLAB_PAT expanded onto
    # each host in GITLAB_ALLOWED_HOSTS. Not a second allowlist.
    gitlab_host_pats: str = Field(
        default="",
        description='JSON object mapping hostname → PAT, e.g. {"gitlab.com":"glpat-…"}',
    )
    gitlab_pat: str = Field(
        default="",
        description="Leftover single GitLab PAT (expanded onto GITLAB_ALLOWED_HOSTS when map empty)",
    )
    # Leftover expander only. Ignored when GITLAB_HOST_PATS is set.
    gitlab_allowed_hosts: str = Field(
        default="",
        description="Leftover comma-separated hosts for a lone GITLAB_PAT (not a separate allowlist)",
    )
    # GitLab MR comment webhook (CE + EE; project-level Note hook on all plans)
    gitlab_webhook_enabled: bool = Field(
        default=False,
        description="Accept GitLab Note and Merge Request webhooks on /yaver/webhook/gitlab",
    )
    gitlab_webhook_secret: str = Field(
        default="",
        description="Shared secret; must match GitLab hook X-Gitlab-Token (empty = accept all)",
    )
    gitlab_trigger_user: str = Field(
        default="",
        description=(
            "Comma-separated GitLab usernames. Mention one on an MR comment "
            "to start a job. Comments from these users are ignored."
        ),
    )
    gitlab_bot_mentions: str = Field(
        default="",
        description=(
            "Leftover. Used only when GITLAB_TRIGGER_USER is empty."
        ),
    )
    gitlab_bot_usernames: str = Field(
        default="",
        description=(
            "Leftover. Used only when GITLAB_TRIGGER_USER and "
            "GITLAB_BOT_MENTIONS are empty."
        ),
    )
    # Azure DevOps Server 2022.2 (on-prem TFS) — same PAT shape as GitLab.
    # First-class: per-host PATs as JSON. A host with a PAT is allowed.
    #   AZURE_HOST_PATS={"tfs.example.com":"…","tfs.internal:8080":"…"}
    # Leftover (only when the JSON map is empty): one AZURE_PAT expanded onto
    # each host in AZURE_ALLOWED_HOSTS. Not a second allowlist.
    # Clone/push/MR use this PAT as Basic pat:<PAT> (IIS rejects empty user).
    azure_host_pats: str = Field(
        default="",
        description='JSON object mapping hostname → Azure PAT, e.g. {"tfs.example.com":"…"}',
    )
    azure_pat: str = Field(
        default="",
        description="Leftover single Azure PAT (expanded onto AZURE_ALLOWED_HOSTS when map empty)",
    )
    azure_allowed_hosts: str = Field(
        default="",
        description="Leftover comma-separated hosts for a lone AZURE_PAT (not a separate allowlist)",
    )
    azure_webhook_enabled: bool = Field(
        default=False,
        description="Accept Azure DevOps Server service hooks on /yaver/webhook/azure",
    )
    azure_webhook_secret: str = Field(
        default="",
        description="Leftover. Ignored. Azure webhooks have no secret.",
    )
    azure_trigger_user: str = Field(
        default="",
        description=(
            "Comma-separated Azure DevOps display names or unique names. "
            "Mention one on a pull-request comment to start a job. "
            "Comments from these users are ignored."
        ),
    )
    azure_bot_mentions: str = Field(
        default="",
        description=(
            "Leftover. Used only when AZURE_TRIGGER_USER is empty."
        ),
    )
    
    # OpenCode agent for Mode: build (and other implementation jobs).
    # OpenCoderman derman-build, not stock OpenCode ``build``.
    default_agent: str = Field(
        default="derman-build",
        description="OpenCode agent for build jobs (opencoderman derman-build)",
    )
    # OpenCode agent for Mode: plan. OpenCoderman derman-plan, not stock ``plan``.
    default_plan_agent: str = Field(
        default="derman-plan",
        description="OpenCode agent for plan jobs (opencoderman derman-plan)",
    )
    default_test_agent: str = Field(
        default="derman-test",
        description="OpenCode agent for test jobs (opencoderman derman-test)",
    )

    # Mode prompts (agent name does not change prompt text)
    agent_prompts_dir: Path = Field(
        default=Path("agent"),
        description="Directory with PLAN_PROMPT.md, BUILD_PROMPT.md, TEST_PROMPT.md",
    )
    plan_prompt_file: Optional[Path] = Field(
        default=None,
        description="Plan-mode prompt (default: {agent_prompts_dir}/PLAN_PROMPT.md)",
    )
    build_prompt_file: Optional[Path] = Field(
        default=None,
        description="Build-mode prompt (default: {agent_prompts_dir}/BUILD_PROMPT.md)",
    )
    test_prompt_file: Optional[Path] = Field(
        default=None,
        description="Test-mode prompt (default: {agent_prompts_dir}/TEST_PROMPT.md)",
    )
    
    # How many agent jobs run at once (raise for large boards / many subtasks)
    max_concurrent_jobs: int = Field(
        default=6,
        description="Max concurrent agent jobs (1–32; also writable on dashboard)",
    )
    # Parallel Jira transitions / dispatch inside one poll cycle
    poll_dispatch_workers: int = Field(
        default=8,
        description="Thread pool size for dispatching issues after a poll",
    )
    # Board poller interval
    poll_interval_seconds: int = Field(default=30)

    # Ops dashboard (FastAPI UI). Intentional product defaults (not a security bug):
    # no auth in v1 + bind 0.0.0.0 + allow_remote so LAN / offline install works.
    # Operators on untrusted networks: DASHBOARD_HOST=127.0.0.1 and/or
    # DASHBOARD_ALLOW_REMOTE=false. See AGENTS.md §3b.
    dashboard_host: str = Field(
        default="0.0.0.0",
        description=(
            "Dashboard bind host. Default 0.0.0.0 (all interfaces) is intentional; "
            "use 127.0.0.1 to lock down."
        ),
    )
    dashboard_port: int = Field(default=8080, description="Dashboard HTTP port")
    dashboard_enabled: bool = Field(default=True, description="Serve ops dashboard with the daemon")
    dashboard_allow_remote: bool = Field(
        default=True,
        description=(
            "If false, non-loopback dashboard_host is forced back to 127.0.0.1. "
            "Default true is intentional so DASHBOARD_HOST=0.0.0.0 works out of the box."
        ),
    )
    dashboard_username: str = Field(
        default="",
        description="Ops dashboard login. Empty with password empty = no login.",
    )
    dashboard_password: str = Field(
        default="",
        description="Ops dashboard login. Does not apply to the poller or GitLab webhooks.",
    )

    # Temp Directory Configuration — per-issue clones are always required
    temp_dir_base: Path = Field(
        default=Path(".temp"),
        description=(
            "Base directory for temp clones. Relative ``.temp`` is remapped to "
            "the durable host default (C:\\vd\\t, /mnt/c/vd/t, /vd/t, or ~/vd/t)."
        ),
    )
    @field_validator("temp_dir_base", mode="after")
    @classmethod
    def _durable_temp_dir(cls, v: Path) -> Path:
        from src.paths import _under_pytest, coerce_win_path, default_temp_dir

        v = coerce_win_path(v)
        if _under_pytest() or v.is_absolute():
            return v
        text = str(v).replace("\\", "/").strip()
        if text in {".temp", "temp", "./.temp"}:
            return default_temp_dir()
        return v

    # Agent / OpenCode Task Configuration (single wall-clock budget for both)
    agent_task_timeout_seconds: int = Field(
        default=1800,
        description=(
            "Wall-clock timeout in seconds for one OpenCode/agent attempt "
            "(orchestrator kill budget == OpenCode run lifetime; default 1800 = 30 min). "
            "Configurable at runtime via dashboard Settings."
        ),
    )
    agent_task_max_retries: int = Field(
        default=3,
        description="Maximum number of retry attempts for failed agent tasks"
    )
    agent_task_retry_delay_seconds: int = Field(
        default=5,
        description="Initial delay in seconds between retry attempts (doubles with each retry)"
    )
    agent_task_retry_backoff_multiplier: float = Field(
        default=2.0,
        description="Multiplier for exponential backoff between retries"
    )
    agent_task_retry_on_timeout: bool = Field(
        default=True,
        description="Whether to retry tasks that timeout"
    )
    agent_task_retry_on_error: bool = Field(
        default=True,
        description="Whether to retry tasks that fail with errors"
    )
    agent_task_max_incomplete_retries: int = Field(
        default=256,
        description=(
            "Extra retry budget when a serve session is incomplete after compact "
            "is waited out. Independent of agent_task_max_retries. "
            "0 = do not retry incomplete beyond max_retries."
        ),
    )
    # Git clone hard timeout — large monorepos + many remotes need a high ceiling
    git_clone_timeout_seconds: int = Field(
        default=1800,
        description=(
            "Max seconds for git clone (hard kill; default 1800 = 30 minutes). "
            "Raise further for very large repositories."
        ),
    )
    # Submodule init/update (often slower than parent clone when many nested modules)
    git_submodule_timeout_seconds: int = Field(
        default=1800,
        description=(
            "Max seconds for git submodule update --init --recursive "
            "(hard kill; default 1800 = 30 minutes). Applied after clone and "
            "again after work-branch checkout."
        ),
    )
    git_update_submodules: bool = Field(
        default=True,
        description=(
            "After clone (and after work-branch checkout), run "
            "`git submodule update --init --recursive`. Disable only if "
            "target repos never use submodules."
        ),
    )
    # Push / fetch / merge / glab MR — hung network ops must not pin job slots forever
    git_command_timeout_seconds: int = Field(
        default=300,
        description=(
            "Max seconds for non-clone git and glab subprocesses "
            "(push, fetch, MR create; default 5 minutes)"
        ),
    )
    
    # One Jira trigger list: To Do assignee intake and @mention / wiki mention.
    # Empty default — never seed the Settings UI with .env.example names.
    jira_trigger_user: str = Field(
        default="",
        description=(
            "Comma-separated Jira names. Used for To Do assignee intake and "
            "for comments that @mention the bot"
        ),
    )
    # Leftover stores. Used only when JIRA_TRIGGER_USER is empty.
    trigger_mentions: str = Field(default="")
    trigger_assignee_names: str = Field(
        default="",
        description="Leftover. Used only when JIRA_TRIGGER_USER is empty.",
    )
    
    @property
    def full_plans_dir(self) -> Path:
        """Durable plans live under ``{YAVER_DATA_DIR}/plans``, not the clone."""
        from src.paths import plans_dir

        return plans_dir()
    
    @property
    def state_dir(self) -> Path:
        from src.paths import agent_subdir

        return agent_subdir("state")
    
    @property
    def jira_projects_list(self) -> List[str]:
        """Get JIRA projects as a list."""
        if not self.jira_projects:
            return ["PROJ"]
        return [p.strip() for p in self.jira_projects.split(",") if p.strip()]
    
    def resolved_jira_trigger_user(self) -> str:
        """Jira trigger list: ``JIRA_TRIGGER_USER`` then leftover names. No ``@``."""
        for raw in (
            self.jira_trigger_user,
            self.trigger_assignee_names,
            self.trigger_mentions,
        ):
            text = format_trigger_users(raw)
            if text:
                return text
        return ""

    def resolved_gitlab_trigger_user(self) -> str:
        """GitLab trigger list: ``GITLAB_TRIGGER_USER`` then leftovers. No ``@``."""
        for raw in (
            self.gitlab_trigger_user,
            self.gitlab_bot_mentions,
            getattr(self, "gitlab_bot_usernames", "") or "",
        ):
            text = format_trigger_users(raw)
            if text:
                return text
        return ""

    def resolved_azure_trigger_user(self) -> str:
        """Azure trigger list: ``AZURE_TRIGGER_USER`` then leftover mentions. No ``@``."""
        for raw in (self.azure_trigger_user, self.azure_bot_mentions):
            text = format_trigger_users(raw)
            if text:
                return text
        return ""

    @property
    def jira_trigger_user_list(self) -> List[str]:
        """Jira bot name fragments (lowercase, no leading @)."""
        names = _jira_bot_names(self.resolved_jira_trigger_user())
        if not names:
            return list(_FALLBACK_TRIGGER_ASSIGNEE_NAMES)
        return [n.lower() for n in names]

    @property
    def trigger_assignee_names_list(self) -> List[str]:
        """Alias of ``jira_trigger_user_list`` (leftover name)."""
        return list(self.jira_trigger_user_list)

    @property
    def gitlab_allowed_hosts_list(self) -> List[str]:
        """Hosts that have a GitLab PAT (lowercase). A host with a PAT is allowed."""
        return sorted(self.gitlab_host_pat_map().keys())

    def gitlab_host_pat_map(self) -> Dict[str, str]:
        """Resolved hostname → PAT map (prefer ``gitlab_host_pats`` JSON).

        A host in this map is allowed. Leftover fallback: if the JSON map is
        empty and ``gitlab_pat`` is set, each host in leftover
        ``gitlab_allowed_hosts`` gets that same PAT.
        """
        out: Dict[str, str] = {}
        raw = (self.gitlab_host_pats or "").strip()
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    for k, v in data.items():
                        host = str(k or "").strip().lower()
                        pat = str(v or "").strip()
                        if host and pat:
                            out[host] = pat
            except json.JSONDecodeError:
                logger.warning("GITLAB_HOST_PATS is not valid JSON; ignoring map")

        if out:
            return out

        # Legacy single PAT + host list
        pat = (self.gitlab_pat or "").strip()
        if not pat:
            return {}
        hosts = [
            h.strip().lower()
            for h in (self.gitlab_allowed_hosts or "").split(",")
            if h.strip()
        ]
        return {h: pat for h in hosts}

    def gitlab_pat_for_host(self, host: str) -> str:
        """Return the PAT for ``host`` (exact hostname[:port] only).

        Parent-domain matching is intentionally not used: it would send the
        PAT to ``evil.gitlab.company.com`` when ``gitlab.company.com`` is
        configured. Add each host (including ``host:port``) in Settings.
        """
        h = (host or "").strip().lower()
        if not h:
            return ""
        mapping = self.gitlab_host_pat_map()
        if not mapping:
            return ""
        if h in mapping:
            return mapping[h]
        # Settings used to persist hostname without :port. Same host.
        if ":" in h:
            name = h.rsplit(":", 1)[0]
            if name in mapping:
                return mapping[name]
        return ""

    def gitlab_has_any_pat(self) -> bool:
        """True if at least one host has a configured PAT."""
        return bool(self.gitlab_host_pat_map())

    def set_gitlab_host_pat_map(self, mapping: Dict[str, str]) -> None:
        """Persist host→PAT map. Allowed hosts are the keys (a PAT allows the host)."""
        cleaned: Dict[str, str] = {}
        for k, v in (mapping or {}).items():
            host = str(k or "").strip().lower()
            pat = str(v or "").strip()
            if host and pat:
                cleaned[host] = pat
        self.gitlab_host_pats = json.dumps(cleaned, separators=(",", ":")) if cleaned else ""
        # Keep leftover field aligned so it cannot disagree with the map in-memory.
        self.gitlab_allowed_hosts = ",".join(sorted(cleaned.keys()))
        # Leftover single PAT: keep only when exactly one host (avoids wrong-host use)
        if len(cleaned) == 1:
            self.gitlab_pat = next(iter(cleaned.values()))
        else:
            self.gitlab_pat = ""

    def all_gitlab_pats(self) -> List[str]:
        """All configured PAT values (for log redaction)."""
        return list(dict.fromkeys(self.gitlab_host_pat_map().values()))

    @property
    def azure_allowed_hosts_list(self) -> List[str]:
        """Hosts that have an Azure PAT (lowercase). A host with a PAT is allowed."""
        return sorted(self.azure_host_pat_map().keys())

    def azure_host_pat_map(self) -> Dict[str, str]:
        """Resolved hostname → Azure PAT map (prefer ``azure_host_pats`` JSON).

        Same leftover rule as GitLab: if the JSON map is empty and
        ``azure_pat`` is set, each host in leftover ``azure_allowed_hosts``
        gets that same PAT.
        """
        out: Dict[str, str] = {}
        raw = (self.azure_host_pats or "").strip()
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    for k, v in data.items():
                        host = str(k or "").strip().lower()
                        pat = str(v or "").strip()
                        if host and pat:
                            out[host] = pat
            except json.JSONDecodeError:
                logger.warning("AZURE_HOST_PATS is not valid JSON; ignoring map")

        if out:
            return out

        pat = (self.azure_pat or "").strip()
        if not pat:
            return {}
        hosts = [
            h.strip().lower()
            for h in (self.azure_allowed_hosts or "").split(",")
            if h.strip()
        ]
        return {h: pat for h in hosts}

    def azure_pat_for_host(self, host: str) -> str:
        """Return the Azure PAT for ``host`` (exact hostname[:port] only)."""
        h = (host or "").strip().lower()
        if not h:
            return ""
        mapping = self.azure_host_pat_map()
        if not mapping:
            return ""
        if h in mapping:
            return mapping[h]
        # Settings used to persist hostname without :port. Same host.
        if ":" in h:
            name = h.rsplit(":", 1)[0]
            if name in mapping:
                return mapping[name]
        return ""

    def azure_has_any_pat(self) -> bool:
        return bool(self.azure_host_pat_map()) or bool((self.azure_pat or "").strip())

    def set_azure_host_pat_map(self, mapping: Dict[str, str]) -> None:
        """Persist host→PAT map. Allowed hosts are the keys (a PAT allows the host)."""
        cleaned: Dict[str, str] = {}
        for k, v in (mapping or {}).items():
            host = str(k or "").strip().lower()
            pat = str(v or "").strip()
            if host and pat:
                cleaned[host] = pat
        self.azure_host_pats = (
            json.dumps(cleaned, separators=(",", ":")) if cleaned else ""
        )
        self.azure_allowed_hosts = ",".join(sorted(cleaned.keys()))
        if len(cleaned) == 1:
            self.azure_pat = next(iter(cleaned.values()))
        else:
            self.azure_pat = ""

    def all_azure_pats(self) -> List[str]:
        pats = list(dict.fromkeys(self.azure_host_pat_map().values()))
        leftover = (self.azure_pat or "").strip()
        if leftover and leftover not in pats:
            pats.append(leftover)
        return pats

    def all_git_pats(self) -> List[str]:
        """GitLab + Azure PATs (log redaction)."""
        return list(dict.fromkeys([*self.all_gitlab_pats(), *self.all_azure_pats()]))
    
    @property
    def prompt_planning(self) -> str:
        """Plan-mode prompt body (PLAN_PROMPT.md)."""
        from src.orchestrator.prompt_builder import PromptBuilder

        return PromptBuilder._load_mode_prompt(
            PromptBuilder.plan_prompt_path(),
            issue_key="ISSUE",
        )

    @property
    def prompt_execution(self) -> str:
        """Build-mode prompt body (BUILD_PROMPT.md)."""
        from src.orchestrator.prompt_builder import PromptBuilder

        return PromptBuilder._load_mode_prompt(
            PromptBuilder.build_prompt_path(),
            issue_key="ISSUE",
        )

    def prompt_commit_policy(
        self,
        issue_key: str,
        *,
        work_branch: Optional[str] = None,
    ) -> str:
        """Issue-keyed git policy from BUILD_PROMPT.md."""
        from src.orchestrator.prompt_builder import PromptBuilder

        return PromptBuilder.commit_message_block(
            issue_key, work_branch=work_branch
        )

    @property
    def trigger_mentions_list(self) -> List[str]:
        """@mention form of the same Jira bot names."""
        return [n if n.startswith("@") else f"@{n}" for n in self.trigger_assignee_names_list]

    @property
    def gitlab_trigger_user_list(self) -> List[str]:
        from src.gitlab.mentions import parse_mention_list

        return parse_mention_list(self.resolved_gitlab_trigger_user())

    @property
    def gitlab_bot_mentions_list(self) -> List[str]:
        return list(self.gitlab_trigger_user_list)

    @property
    def gitlab_bot_usernames_list(self) -> List[str]:
        return list(self.gitlab_trigger_user_list)

    @property
    def azure_trigger_user_list(self) -> List[str]:
        from src.azure.mentions import parse_mention_list

        return parse_mention_list(self.resolved_azure_trigger_user())

    @property
    def azure_bot_mentions_list(self) -> List[str]:
        return list(self.azure_trigger_user_list)

    @property
    def azure_bot_usernames_list(self) -> List[str]:
        return list(self.azure_trigger_user_list)
    
    def is_configured(self) -> bool:
        """Check if required JIRA settings are configured."""
        return all([
            self.jira_host and self.jira_host.strip(),
            self.jira_api_token and self.jira_api_token.strip(),
        ])
    
    def validate_or_raise(self):
        """Validate settings and raise error if not configured."""
        if not self.is_configured():
            missing = []
            if not self.jira_host:
                missing.append("JIRA_HOST")
            if not self.jira_api_token:
                missing.append("JIRA_API_TOKEN")
            raise ValueError(
                f"Missing required configuration: {', '.join(missing)}\n"
                "Please set these in your .env file or environment variables.\n"
                "Run: cp .env.example .env && nano .env"
            )


# Global settings instance - lazy loaded to handle missing config gracefully
_settings: Optional[Settings] = None
_current_temp_dir: Optional[Path] = None

# Dashboard runtime overrides (survive process restart).
# Written by apply_settings_update; applied after Settings() loads env.
# A cwd .env key wins when that file is newer than this field's last save
# (legacy rows without a timestamp never hide a .env value).
_RUNTIME_SETTINGS_NAME = "runtime_settings.json"
_RUNTIME_UPDATED_KEY = "_updated"

# Keys the dashboard may persist (no secrets).
_RUNTIME_PERSIST_KEYS = frozenset(
    {
        "agent_task_timeout_seconds",
        "agent_task_max_retries",
        "agent_task_max_incomplete_retries",
        "poll_interval_seconds",
        "max_concurrent_jobs",
        "jira_board_id",
        "jira_host",
        "jira_email",
        "default_model",
        "agent_backend",
        "project_repositories",
        "trigger_mentions",
        "trigger_assignee_names",
        "jira_trigger_user",
        "gitlab_trigger_user",
        "azure_trigger_user",
        "gitlab_bot_mentions",
        "azure_bot_mentions",
        "gitlab_webhook_enabled",
        "azure_webhook_enabled",
        "azure_collection_urls",
    }
)

# Map Settings field → env var name for os.environ mirror (so re-reads stay consistent).
_RUNTIME_ENV_MIRROR = {
    "agent_task_timeout_seconds": "AGENT_TASK_TIMEOUT_SECONDS",
    "agent_task_max_retries": "AGENT_TASK_MAX_RETRIES",
    "agent_task_max_incomplete_retries": "AGENT_TASK_MAX_INCOMPLETE_RETRIES",
    "poll_interval_seconds": "POLL_INTERVAL_SECONDS",
    "max_concurrent_jobs": "MAX_CONCURRENT_JOBS",
    "jira_board_id": "JIRA_BOARD_ID",
    "jira_host": "JIRA_HOST",
    "jira_email": "JIRA_EMAIL",
    "default_model": "DEFAULT_MODEL",
    "agent_backend": "AGENT_BACKEND",
    "trigger_mentions": "TRIGGER_MENTIONS",
    "trigger_assignee_names": "TRIGGER_ASSIGNEE_NAMES",
    "jira_trigger_user": "JIRA_TRIGGER_USER",
    "gitlab_trigger_user": "GITLAB_TRIGGER_USER",
    "azure_trigger_user": "AZURE_TRIGGER_USER",
    "gitlab_bot_mentions": "GITLAB_BOT_MENTIONS",
    "azure_bot_mentions": "AZURE_BOT_MENTIONS",
    "gitlab_webhook_enabled": "GITLAB_WEBHOOK_ENABLED",
    "azure_webhook_enabled": "AZURE_WEBHOOK_ENABLED",
}


def jira_host_is_cloud(host: Any = None) -> bool:
    """True for Atlassian Cloud (``*.atlassian.net``). Cloud API tokens need Basic."""
    text = str(host if host is not None else "").strip().lower()
    return "atlassian.net" in text


def runtime_settings_path() -> Path:
    """Path to JSON file holding dashboard runtime overrides."""
    from src.paths import agent_data_dir

    dest = agent_data_dir()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return (dest / _RUNTIME_SETTINGS_NAME).resolve()


def _read_runtime_file() -> Dict[str, Any]:
    """Raw runtime JSON (includes ``_updated``); empty dict if missing."""
    path = runtime_settings_path()
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:
        logger.warning(f"Could not load runtime settings {path}: {e}")
        return {}


def load_runtime_settings() -> Dict[str, Any]:
    """Load dashboard runtime overrides from disk (empty dict if missing)."""
    return {
        k: v for k, v in _read_runtime_file().items() if k in _RUNTIME_PERSIST_KEYS
    }


def _runtime_updated_map(raw: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """Per-field unix times from the last dashboard save of that field."""
    blob = raw if raw is not None else _read_runtime_file()
    meta = blob.get(_RUNTIME_UPDATED_KEY)
    if not isinstance(meta, dict):
        return {}
    out: Dict[str, float] = {}
    for key, value in meta.items():
        if key not in _RUNTIME_PERSIST_KEYS:
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def save_runtime_settings(updates: Dict[str, Any]) -> None:
    """Merge *updates* into runtime settings file and mirror into os.environ."""
    path = runtime_settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = _read_runtime_file()
        current = {k: v for k, v in raw.items() if k in _RUNTIME_PERSIST_KEYS}
        updated = _runtime_updated_map(raw)
        now = time.time()
        for key, value in updates.items():
            if key not in _RUNTIME_PERSIST_KEYS:
                continue
            if value is None:
                continue
            current[key] = value
            updated[key] = now
        payload: Dict[str, Any] = dict(current)
        if updated:
            payload[_RUNTIME_UPDATED_KEY] = updated
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _mirror_runtime_to_environ(current)
        logger.info(
            f"Persisted runtime settings to {path}: "
            + ", ".join(f"{k}={current[k]!r}" for k in sorted(updates) if k in current)
        )
    except Exception as e:
        logger.error(f"Could not save runtime settings {path}: {e}")


def _mirror_runtime_to_environ(data: Dict[str, Any]) -> None:
    """Keep os.environ in sync so timeout is not lost if something re-reads env."""
    for key, value in data.items():
        env_name = _RUNTIME_ENV_MIRROR.get(key)
        if not env_name:
            continue
        if isinstance(value, bool):
            os.environ[env_name] = "true" if value else "false"
        else:
            os.environ[env_name] = str(value)


def _first_dotenv_file() -> Optional[Path]:
    """``.env`` next to the process cwd (start scripts and the frozen exe chdir here)."""
    try:
        path = (Path.cwd() / ".env").resolve()
    except OSError:
        return None
    return path if path.is_file() else None


def _dotenv_defined_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return keys
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key:
            keys.add(key)
    return keys


def _runtime_keys_superseded_by_dotenv() -> set[str]:
    """Field names to keep from cwd ``.env`` instead of runtime_settings.json.

    Saving one Settings field must not stamp every other leftover runtime
    value over a later ``.env`` edit. Compare each field's save time.
    Legacy rows (no timestamp) never hide a key that ``.env`` defines.
    """
    env_path = _first_dotenv_file()
    if env_path is None:
        return set()
    env_keys = _dotenv_defined_keys(env_path)
    if not env_keys:
        return set()
    try:
        env_mtime = env_path.stat().st_mtime
    except OSError:
        return set()
    raw = _read_runtime_file()
    if not raw:
        return set()
    updated = _runtime_updated_map(raw)
    skip: set[str] = set()
    for field, env_name in _RUNTIME_ENV_MIRROR.items():
        if env_name not in env_keys:
            continue
        saved_at = updated.get(field)
        if saved_at is None or env_mtime > saved_at:
            skip.add(field)
    return skip


def apply_runtime_settings_to(settings_obj: "Settings") -> None:
    """Apply persisted dashboard overrides onto a Settings instance (after env load)."""
    data = load_runtime_settings()
    if not data:
        return
    skip_keys = _runtime_keys_superseded_by_dotenv()
    for key, value in data.items():
        if key in skip_keys:
            continue
        if not hasattr(settings_obj, key):
            continue
        if key == "jira_board_id":
            text = str(value or "").strip()
            if len(text) >= 2 and text[0] == text[-1] and text[0] in "`'\"":
                text = text[1:-1].strip()
            if not text.isdigit():
                logger.warning(
                    f"Ignoring invalid runtime jira_board_id={value!r} "
                    f"(need digits, e.g. 1); keeping {getattr(settings_obj, key, None)!r}"
                )
                continue
            value = text
        if key == "agent_task_timeout_seconds":
            try:
                value = int(value)
            except (TypeError, ValueError):
                logger.warning(
                    f"Ignoring invalid runtime agent_task_timeout_seconds={value!r}"
                )
                continue
        if key == "jira_email":
            # Cloud API tokens need email+token Basic. An empty runtime
            # override (from an old Settings save) must not wipe .env email.
            host = getattr(settings_obj, "jira_host", "") or data.get("jira_host")
            if jira_host_is_cloud(host) and not str(value or "").strip():
                continue
        if key == "project_repositories":
            from src.dashboard.project_repos import project_repositories_to_json

            value = project_repositories_to_json(value)
        try:
            setattr(settings_obj, key, value)
        except Exception as e:
            logger.warning(f"Could not apply runtime setting {key}={value!r}: {e}")
    applied = {k: v for k, v in data.items() if k not in skip_keys}
    _mirror_runtime_to_environ(applied)
    if applied:
        logger.info(
            "Applied runtime settings overrides: "
            + ", ".join(f"{k}={applied[k]!r}" for k in sorted(applied))
        )
    if skip_keys:
        logger.info(
            "Kept .env values (newer than that field's last Settings save): "
            + ", ".join(sorted(skip_keys))
        )


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        # Per-field runtime overrides; a newer cwd .env key is kept as-is.
        apply_runtime_settings_to(_settings)
    return _settings


def live_agent_timeout_seconds(*, default: int = 1800) -> int:
    """Current OpenCode/agent wall-clock budget (dashboard + runtime + env).

    Re-read on every call so a Settings save of 7200 applies to the in-flight
    serve turn / next retry, not only jobs that started after the save.
    """
    live = get_settings()
    raw = getattr(live, "agent_task_timeout_seconds", None)
    try:
        if raw is None or isinstance(raw, bool):
            return int(default)
        return int(raw)
    except (TypeError, ValueError):
        return int(default)

def set_current_temp_dir(temp_dir: Optional[Path]) -> None:
    global _current_temp_dir
    _current_temp_dir = temp_dir
    logger.debug(f"Current temp directory set to: {temp_dir}")

def get_current_temp_dir() -> Optional[Path]:
    return _current_temp_dir

settings = get_settings()
