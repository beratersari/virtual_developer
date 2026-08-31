"""Configuration management for JIRA Virtual Developer."""

import json
import os
import re
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
        # CWD first (how operators run the daemon), then package root next to src/
        candidates.append(Path.cwd() / ".env")
        candidates.append(Path.cwd() / ".env.agent")
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
            logger.debug(f"Loaded dotenv keys from {path} (applied≈{applied})")
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
        default=128000,
        description=(
            "Job-local OpenCode model context cap (0 = no override). "
            "32k filled in minutes and looped compact/restore; 128k is "
            "enough for a long build without compacting every turn. "
            "Zen free models advertise 190k–1M natively."
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
    
    # Git Configuration (for commits in target project folder)
    git_user_name: str = Field(default="DevBot", description="Git user name for commits")
    git_user_email: str = Field(default="devbot@example.com", description="Git user email for commits")
    
    # GitLab credentials — repository URL and source branch come from each Jira issue
    # (see src/issue_git_spec.py: Repository + Source + Target; MR source → target)
    #
    # Preferred: per-host PATs as JSON object:
    #   GITLAB_HOST_PATS={"gitlab.com":"glpat-…","gitlab.internal.com":"glpat-…"}
    # Legacy (still supported): single GITLAB_PAT + GITLAB_ALLOWED_HOSTS (same PAT for each host)
    gitlab_host_pats: str = Field(
        default="",
        description='JSON object mapping hostname → PAT, e.g. {"gitlab.com":"glpat-…"}',
    )
    gitlab_pat: str = Field(
        default="",
        description="Legacy single GitLab PAT (used with GITLAB_ALLOWED_HOSTS when map empty)",
    )
    # Hosts that may receive GITLAB_PAT (clone/push/MR). Required when legacy PAT is set.
    # Comma-separated hostnames, e.g. "gitlab.example.com,gitlab.com"
    gitlab_allowed_hosts: str = Field(
        default="",
        description="Legacy comma-separated hosts for single GITLAB_PAT (fail-closed when PAT is set)",
    )
    # GitLab MR comment webhook (CE + EE; project-level Note hook on all plans)
    gitlab_webhook_enabled: bool = Field(
        default=False,
        description="Accept GitLab Note webhooks on /webhooks/gitlab",
    )
    gitlab_webhook_secret: str = Field(
        default="",
        description="Shared secret; must match GitLab hook X-Gitlab-Token (empty = accept all)",
    )
    gitlab_bot_mentions: str = Field(
        default="@berat_ai",
        description="Comma-separated @names that trigger a job (e.g. @berat_ai,@DevBot)",
    )
    gitlab_bot_usernames: str = Field(
        default="",
        description=(
            "GitLab usernames of this bot (ignore its own notes to prevent loops). "
            "Defaults to GITLAB_BOT_MENTIONS without @"
        ),
    )
    
    # OpenCode agent name for plan + build runs (oracle consult uses "oracle").
    # Mode (plan vs build) selects the prompt file; agent name does not change prompts.
    default_agent: str = Field(
        default="build",
        description="OpenCode agent for plan and build jobs (stock build; not Sisyphus/Atlas)",
    )

    # Exactly two mode prompts (agent name does not change prompt text)
    agent_prompts_dir: Path = Field(
        default=Path("agent"),
        description="Directory with PLAN_PROMPT.md and BUILD_PROMPT.md",
    )
    plan_prompt_file: Optional[Path] = Field(
        default=None,
        description="Plan-mode prompt (default: {agent_prompts_dir}/PLAN_PROMPT.md)",
    )
    build_prompt_file: Optional[Path] = Field(
        default=None,
        description="Build-mode prompt (default: {agent_prompts_dir}/BUILD_PROMPT.md)",
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
    # Board poller interval (used when jira_intake_mode=poll)
    poll_interval_seconds: int = Field(default=30)
    # poll = board/sprint poller (default). webhook = POST /webhooks/jira only.
    # Switching at runtime via dashboard; default comes from .env.
    jira_intake_mode: str = Field(
        default="poll",
        description="Jira intake: poll (board poller) or webhook (POST /webhooks/jira)",
    )
    jira_webhook_secret: str = Field(
        default="",
        description=(
            "Shared secret for /webhooks/jira. Jira Server 9.4 has no HMAC — "
            "put the token in the hook URL (?token=). Cloud may send X-Hub-Signature."
        ),
    )

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
    
    # Trigger Configuration - stored as strings, parsed as properties
    trigger_on_assignment: bool = Field(default=True)
    trigger_labels: str = Field(default="ai-assist,bot")
    # Optional @mention strings for free-form comment commands (not board intake)
    trigger_mentions: str = Field(default="@DevBot,@AI")
    # Substrings matched against assignee displayName / name / key (case-insensitive)
    trigger_assignee_names: str = Field(
        default="jira ai bot,jira-ai-bot,jiraai,devbot",
        description=(
            "Comma-separated name fragments; issue is bot-assigned when any "
            "fragment appears in assignee displayName, name, or key"
        ),
    )
    
    @property
    def full_plans_dir(self) -> Path:
        return Path.cwd() / self.sisyphus_plans_dir
    
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
    
    @property
    def trigger_labels_list(self) -> List[str]:
        """Get trigger labels as a list."""
        if not self.trigger_labels:
            return ["ai-assist", "bot"]
        return [item.strip() for item in self.trigger_labels.split(",") if item.strip()]

    @property
    def trigger_assignee_names_list(self) -> List[str]:
        """Assignee name fragments for bot-assignment trigger (lowercase)."""
        raw = (self.trigger_assignee_names or "").strip()
        if not raw:
            return ["jira ai bot", "jira-ai-bot", "jiraai", "devbot"]
        return [item.strip().lower() for item in raw.split(",") if item.strip()]

    @property
    def gitlab_allowed_hosts_list(self) -> List[str]:
        """Hosts that have (or are allowed) a GitLab PAT (lowercase)."""
        return sorted(self.gitlab_host_pat_map().keys())

    def gitlab_host_pat_map(self) -> Dict[str, str]:
        """Resolved hostname → PAT map (prefer ``gitlab_host_pats`` JSON).

        Legacy fallback: if the JSON map is empty and ``gitlab_pat`` is set,
        each host in ``gitlab_allowed_hosts`` gets that same PAT.
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
        return ""

    def gitlab_has_any_pat(self) -> bool:
        """True if at least one host has a configured PAT."""
        return bool(self.gitlab_host_pat_map())

    def set_gitlab_host_pat_map(self, mapping: Dict[str, str]) -> None:
        """Persist host→PAT map into runtime settings (JSON + legacy mirrors)."""
        cleaned: Dict[str, str] = {}
        for k, v in (mapping or {}).items():
            host = str(k or "").strip().lower()
            pat = str(v or "").strip()
            if host and pat:
                cleaned[host] = pat
        self.gitlab_host_pats = json.dumps(cleaned, separators=(",", ":")) if cleaned else ""
        # Mirror for older code paths / display
        self.gitlab_allowed_hosts = ",".join(sorted(cleaned.keys()))
        # Legacy single PAT: keep only when exactly one host (avoids wrong-host use)
        if len(cleaned) == 1:
            self.gitlab_pat = next(iter(cleaned.values()))
        else:
            self.gitlab_pat = ""

    def all_gitlab_pats(self) -> List[str]:
        """All configured PAT values (for log redaction)."""
        return list(dict.fromkeys(self.gitlab_host_pat_map().values()))
    
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
        """Get trigger mentions as a list."""
        if not self.trigger_mentions:
            return ["@DevBot", "@AI"]
        return [item.strip() for item in self.trigger_mentions.split(",") if item.strip()]

    @property
    def jira_intake_mode_normalized(self) -> str:
        """``poll`` or ``webhook`` (default poll)."""
        return _normalize_intake_mode(self.jira_intake_mode)

    @property
    def gitlab_bot_mentions_list(self) -> List[str]:
        from src.gitlab.mentions import parse_mention_list

        return parse_mention_list(self.gitlab_bot_mentions)

    @property
    def gitlab_bot_usernames_list(self) -> List[str]:
        from src.gitlab.mentions import parse_mention_list

        names = parse_mention_list(self.gitlab_bot_usernames)
        return names or list(self.gitlab_bot_mentions_list)
    
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

# Dashboard runtime overrides (survive process restart; win over .env).
# Written by apply_settings_update; applied after Settings() loads env.
_RUNTIME_SETTINGS_NAME = "runtime_settings.json"

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
        "trigger_labels",
        "trigger_on_assignment",
        "default_model",
        "agent_backend",
        "project_repositories",
        "jira_intake_mode",
        "trigger_mentions",
        "trigger_assignee_names",
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
    "trigger_labels": "TRIGGER_LABELS",
    "trigger_on_assignment": "TRIGGER_ON_ASSIGNMENT",
    "default_model": "DEFAULT_MODEL",
    "agent_backend": "AGENT_BACKEND",
    "jira_intake_mode": "JIRA_INTAKE_MODE",
    "trigger_mentions": "TRIGGER_MENTIONS",
    "trigger_assignee_names": "TRIGGER_ASSIGNEE_NAMES",
}


def jira_host_is_cloud(host: Any = None) -> bool:
    """True for Atlassian Cloud (``*.atlassian.net``). Cloud API tokens need Basic."""
    text = str(host if host is not None else "").strip().lower()
    return "atlassian.net" in text


def _normalize_intake_mode(raw: Any) -> str:
    """``poll`` or ``webhook``. Local so Settings bootstrap never imports jira."""
    text = str(raw or "").strip().lower()
    if text in {"webhook", "webhooks", "hook", "push", "http"}:
        return "webhook"
    return "poll"


def runtime_settings_path() -> Path:
    """Path to JSON file holding dashboard runtime overrides."""
    from src.paths import agent_data_dir

    dest = agent_data_dir()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return (dest / _RUNTIME_SETTINGS_NAME).resolve()


def load_runtime_settings() -> Dict[str, Any]:
    """Load dashboard runtime overrides from disk (empty dict if missing)."""
    path = runtime_settings_path()
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in _RUNTIME_PERSIST_KEYS}
    except Exception as e:
        logger.warning(f"Could not load runtime settings {path}: {e}")
        return {}


def save_runtime_settings(updates: Dict[str, Any]) -> None:
    """Merge *updates* into runtime settings file and mirror into os.environ."""
    path = runtime_settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        current = load_runtime_settings()
        for key, value in updates.items():
            if key not in _RUNTIME_PERSIST_KEYS:
                continue
            if value is None:
                continue
            current[key] = value
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
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


def apply_runtime_settings_to(settings_obj: "Settings") -> None:
    """Apply persisted dashboard overrides onto a Settings instance (after env load)."""
    data = load_runtime_settings()
    if not data:
        return
    for key, value in data.items():
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
        if key == "jira_intake_mode":
            value = _normalize_intake_mode(value)
        if key == "project_repositories":
            from src.dashboard.project_repos import project_repositories_to_json

            value = project_repositories_to_json(value)
        try:
            setattr(settings_obj, key, value)
        except Exception as e:
            logger.warning(f"Could not apply runtime setting {key}={value!r}: {e}")
    _mirror_runtime_to_environ(data)
    logger.info(
        "Applied runtime settings overrides: "
        + ", ".join(f"{k}={data[k]!r}" for k in sorted(data))
    )


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        # Dashboard overrides win over .env so agent timeout changes stick.
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
