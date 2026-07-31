"""Configuration management for JIRA Virtual Developer."""

import os
from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.logger import logger


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    # JIRA Configuration
    # - On-prem Server/DC PAT: set JIRA_HOST + JIRA_API_TOKEN (Bearer)
    # - Jira Cloud API token: set JIRA_HOST + JIRA_EMAIL + JIRA_API_TOKEN (Basic email:token)
    jira_host: str = Field(default="", description="JIRA instance URL")
    jira_email: str = Field(
        default="",
        description="Atlassian account email (required for Jira Cloud API tokens; unused for on-prem Bearer PAT)",
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
    
    # Webhook Configuration
    webhook_port: int = Field(default=3000)
    webhook_path: str = Field(default="/webhook/jira")
    webhook_secret: Optional[str] = Field(default=None)
    
    # Oh My OpenAgent Configuration
    opencode_cli: str = Field(default="opencode", description="OpenCode CLI command")
    project_root: Path = Field(default=Path.cwd(), description="Project root directory")
    sisyphus_plans_dir: Path = Field(default=Path(".sisyphus/plans"))
    default_model: str = Field(default="ollama/Qwen3.5-397B-A17B-FP8", description="Default model for agent tasks")
    
    # Git Configuration (for commits in target project folder)
    git_user_name: str = Field(default="DevBot", description="Git user name for commits")
    git_user_email: str = Field(default="devbot@example.com", description="Git user email for commits")
    
    # Default branch to checkout before creating feature branches (optional)
    # If not set, falls back to 'main', then logs a message if neither exists
    default_branch: str = Field(default="develop", description="Default branch to checkout before creating feature branches")
    
    # Remote GitLab repository (optional)
    project_gitlab_url: str = Field(default="", description="GitLab repo URL to clone into PROJECT_ROOT")
    gitlab_pat: str = Field(default="", description="GitLab Personal Access Token for push/merge-request")
    
    # Agent Configuration
    default_agent: str = Field(default="sisyphus")
    planning_agent: str = Field(default="prometheus")
    orchestrator_agent: str = Field(default="atlas")
    execution_category: str = Field(default="deep")

    # -------------------------------------------------------------------------
    # System Prompts — loaded from markdown files with fallback to inline defaults.
    # Configure file paths via environment variables or keep defaults.
    # -------------------------------------------------------------------------
    prompt_planning_file: Path = Field(
        default=Path("agent/prompts/PLANNING.md"),
        description="Path to planning prompt markdown file",
    )
    
    prompt_execution_file: Path = Field(
        default=Path("agent/prompts/EXECUTION.md"),
        description="Path to execution prompt markdown file",
    )
    
    prompt_direct_execution_file: Path = Field(
        default=Path("agent/prompts/DIRECT_EXECUTION.md"),
        description="Path to direct execution prompt markdown file",
    )
    
    prompt_oracle_file: Path = Field(
        default=Path("agent/prompts/ORACLE.md"),
        description="Path to oracle prompt markdown file",
    )
    
    # Code Review Configuration
    code_review_agent: str = Field(
        default="explore",
        description="Agent to use for code review (uses read-only review prompt)"
    )
    code_review_model: str = Field(
        default="",
        description="Model to use for code review via oh-my-openagent"
    )
    code_review_prompt_file: Path = Field(
        default=Path("agent/rules/CODE_REVIEW.md"),
        description="Path to code review prompt markdown file"
    )

    # Feature Flags
    auto_start_plans: bool = Field(default=False)
    max_concurrent_jobs: int = Field(default=3)
    enable_webhook: bool = Field(default=True)
    enable_polling: bool = Field(default=False)
    poll_interval_seconds: int = Field(default=30)

    # Temp Directory Configuration — per-issue clones are always required
    temp_dir_base: Path = Field(
        default=Path(".temp"),
        description="Base directory for temp working folders (relative to agent root)"
    )
    temp_dir_format: str = Field(
        default="{remote_name}_{jira_issue_id}_{timestamp}",
        description="Temp folder naming format. Available: {remote_name}, {jira_issue_id}, {timestamp}, {uuid}"
    )
    temp_cleanup_policy: str = Field(
        default="never",
        description="Temp folder cleanup policy: 'always', 'on_success', 'never'"
    )
    
    # Agent Task Configuration
    agent_task_timeout_seconds: int = Field(
        default=1800,
        description="Maximum time in seconds for an agent task to complete (default: 30 minutes)"
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
    
    # Redis / Celery
    redis_url: str = Field(default="redis://localhost:6379/0")
    
    # Logging
    log_level: str = Field(default="INFO")
    log_file: Optional[Path] = Field(default=Path("logs/jira-agent.log"))
    
    # Trigger Configuration - stored as strings, parsed as properties
    trigger_on_assignment: bool = Field(default=True)
    trigger_labels: str = Field(default="ai-assist,bot")
    trigger_mentions: str = Field(default="@DevBot,@AI")
    
    @property
    def full_plans_dir(self) -> Path:
        return Path.cwd() / self.sisyphus_plans_dir
    
    @property
    def state_dir(self) -> Path:
        from pathlib import Path as PathLib
        return PathLib.cwd() / ".jira-agent" / "state"
    
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
    def prompt_planning(self) -> str:
        """Load planning prompt from file if it exists, otherwise use default."""
        return self._load_prompt_from_file(
            self.prompt_planning_file,
            self._get_default_planning_prompt(),
            "planning"
        )
    
    @property
    def prompt_execution(self) -> str:
        """Load execution prompt from file if it exists, otherwise use default."""
        return self._load_prompt_from_file(
            self.prompt_execution_file,
            self._get_default_execution_prompt(),
            "execution"
        )
    
    @property
    def prompt_direct_execution(self) -> str:
        """Load direct execution prompt from file if it exists, otherwise use default."""
        return self._load_prompt_from_file(
            self.prompt_direct_execution_file,
            self._get_default_direct_execution_prompt(),
            "direct execution"
        )
    
    @property
    def prompt_oracle(self) -> str:
        """Load oracle prompt from file if it exists, otherwise use default."""
        return self._load_prompt_from_file(
            self.prompt_oracle_file,
            self._get_default_oracle_prompt(),
            "oracle"
        )
    
    def _load_prompt_from_file(self, prompt_file: Path, default_prompt: str, prompt_name: str) -> str:
        from pathlib import Path as PathLib
        import os
        
        cwd = PathLib.cwd()
        logger.info(f"Loading {prompt_name} prompt from file")
        logger.debug(f"Current working directory: {cwd}")
        logger.debug(f"Input prompt_file parameter: {prompt_file}")
        
        paths_to_try = []
        
        temp_prompt_path = cwd / "agent" / "prompts" / prompt_file.name
        paths_to_try.append(("temp_dir", temp_prompt_path))
        
        vd_prompt_path = cwd / "agent" / "prompts" / prompt_file.name
        paths_to_try.append(("vd_project", vd_prompt_path))
        
        env_var_name = f"PROMPT_{prompt_name.upper().replace(' ', '_')}_FILE"
        env_path = os.environ.get(env_var_name)
        if env_path:
            paths_to_try.insert(0, ("env_var", PathLib(env_path)))
            logger.debug(f"Found env var {env_var_name}: {env_path}")
        else:
            logger.debug(f"No env var {env_var_name} set")
        
        if prompt_file.is_absolute():
            paths_to_try.insert(0, ("absolute", prompt_file))
        else:
            paths_to_try.insert(0, ("relative_cwd", cwd / prompt_file))
        
        logger.debug(f"Will try {len(paths_to_try)} paths in order:")
        for i, (source, path) in enumerate(paths_to_try, 1):
            exists = "✓ EXISTS" if path.exists() else "✗ NOT FOUND"
            is_file = " (file)" if path.exists() and path.is_file() else ""
            is_dir = " (dir)" if path.exists() and path.is_dir() else ""
            logger.debug(f"  {i}. [{source}] {path} {exists}{is_file}{is_dir}")
        
        loaded_from = None
        for source, path in paths_to_try:
            logger.debug(f"Trying prompt path: {path} (source: {source})")
            
            if not path.exists():
                logger.debug(f"  Path does not exist: {path}")
                continue
            
            if not path.is_file():
                logger.debug(f"  Path exists but is not a file (is_dir={path.is_dir()}): {path}")
                continue
            
            try:
                content = path.read_text(encoding="utf-8")
                file_size = len(content)
                line_count = len(content.splitlines())
                logger.info(f"Successfully loaded {prompt_name} prompt: {file_size} bytes, {line_count} lines from {source}")
                loaded_from = path
                return content
            except Exception as e:
                logger.error(f"Error reading {prompt_name} prompt from {path}: {e}")
                continue
        
        logger.warning(
            f"{prompt_name.capitalize()} prompt file not found in any location. "
            f"Using default inline prompt ({len(default_prompt)} chars)."
        )
        return default_prompt
    
    def _get_default_planning_prompt(self) -> str:
        """Default planning prompt (fallback if file not found)."""
        return (
            "As Prometheus, create a comprehensive work plan for this JIRA issue.\n"
            "\n"
            "1. **Interview Mode**: Ask clarifying questions if requirements are ambiguous\n"
            "2. **Research**: Explore the codebase to understand existing patterns\n"
            "3. **Plan Generation**: Create a detailed plan with:\n"
            "   - Task breakdown with checkboxes\n"
            "   - File references and locations\n"
            "   - Implementation approach\n"
            "   - Testing strategy\n"
            "   - Estimated effort\n"
            "\n"
            "Output the plan to the designated plan file."
        )
    
    def _get_default_execution_prompt(self) -> str:
        """Default execution prompt (fallback if file not found)."""
        return (
            "## Delegation Guidelines\n"
            '- Use `category="visual-engineering"` for UI/UX work\n'
            '- Use `category="deep"` for complex problem-solving\n'
            '- Use `category="quick"` for simple fixes\n'
            '- Use `subagent_type="oracle"` for architecture decisions\n'
            '- Use `subagent_type="explore"` for codebase research\n'
            "\n"
            "## Success Criteria\n"
            "- All plan checkboxes checked\n"
            "- Tests passing\n"
            "- No type errors\n"
            "- Code follows project conventions"
        )
    
    def _get_default_direct_execution_prompt(self) -> str:
        """Default direct execution prompt (fallback if file not found)."""
        return (
            "## Instructions\n"
            "1. Analyze the task and current codebase\n"
            "2. Create todos for multi-step work\n"
            "3. Implement the solution following existing patterns\n"
            "4. Run verification (tests, type checking)\n"
            "5. **COMMIT YOUR CHANGES**: If you modified any code files, you MUST commit with a meaningful message\n"
            "6. Report completion with summary of changes and commit hash\n"
            "\n"
            "## Commit Requirements\n"
            "- ALWAYS commit after making code changes - this is MANDATORY\n"
            "- Use Conventional Commits format: feat:, fix:, chore:, docs:, refactor:, test:\n"
            "- Include the JIRA issue key in the commit message\n"
            "- Do NOT commit .env, credentials, or secret files\n"
            "\n"
            "## Constraints\n"
            "- Follow existing code style\n"
            "- Add tests for new functionality\n"
            "- Do not break existing tests\n"
            "- Minimal, focused changes"
        )
    
    def _get_default_oracle_prompt(self) -> str:
        """Default oracle prompt (fallback if file not found)."""
        return (
            "## Response Format\n"
            "1. **Direct Answer**: Clear response to the question\n"
            "2. **Rationale**: Why this approach is recommended\n"
            "3. **Alternatives**: Other options considered\n"
            "4. **Trade-offs**: Pros/cons of each approach\n"
            "5. **Implementation Hints**: Key files/patterns to use\n"
            "\n"
            "Be thorough but concise. Focus on practical guidance."
        )
    
    @property
    def prompt_code_review(self) -> str:
        from pathlib import Path as PathLib
        import os
        
        prompt_file = self.code_review_prompt_file
        cwd = PathLib.cwd()
        prompt_name = "code review"
        current_temp = get_current_temp_dir()
        
        logger.debug(f"========== Loading {prompt_name.upper()} PROMPT ==========")
        logger.debug(f"Current working directory: {cwd}")
        logger.debug(f"Active temp directory: {current_temp}")
        logger.debug(f"code_review_prompt_file: {prompt_file}")
        
        paths_to_try = []
        
        if current_temp:
            temp_project_path = current_temp / "agent" / "rules" / prompt_file.name
            paths_to_try.append(("temp_project", temp_project_path))
        
        vd_project_path = cwd / "agent" / "rules" / prompt_file.name
        paths_to_try.append(("vd_project", vd_project_path))
        
        env_path = os.environ.get("PROMPT_CODE_REVIEW_FILE")
        if env_path:
            paths_to_try.insert(0, ("env_var", PathLib(env_path)))
            logger.debug(f"Found env var PROMPT_CODE_REVIEW_FILE: {env_path}")
        
        logger.debug(f"Will try {len(paths_to_try)} paths in order:")
        for i, (source, path) in enumerate(paths_to_try, 1):
            exists = "EXISTS" if path.exists() else "NOT FOUND"
            logger.debug(f"  {i}. [{source}] {path} {exists}")
        
        for source, path in paths_to_try:
            logger.debug(f"Trying: {path} (source: {source})")
            
            if not path.exists():
                logger.debug(f"  Path does not exist")
                continue
            
            if not path.is_file():
                logger.debug(f"  Path exists but is not a file (is_dir={path.is_dir()})")
                continue
            
            try:
                content = path.read_text(encoding="utf-8")
                file_size = len(content)
                line_count = len(content.splitlines())
                logger.info(f"SUCCESS! Loaded {file_size} bytes, {line_count} lines from {path}")
                logger.debug(f"========== {prompt_name.upper()} PROMPT LOADED ==========")
                return content
            except Exception as e:
                logger.error(f"Error reading file: {e}")
                continue
        
        logger.warning(f"ALL PATHS FAILED for {prompt_name} prompt - Using default inline prompt")
        logger.debug(f"========== {prompt_name.upper()} PROMPT DEFAULT USED ==========")
        return ("""
           ---
name: cpp-review
description: Ruthless C++ code review for correctness, lifetime safety, ownership, concurrency, performance, and maintainability.
---

# C++ Review Skill

You are performing a **code review** on the changes that were just made for this JIRA issue.\nThis is a **read-only review** — do NOT make any edits or changes to the code.

### Review Steps

1. **Examine Changes**: Run `git diff HEAD~1` (or `git log --oneline -5` then diff) to see what was changed
2. **Read Modified Files**: Read the full content of any modified files to understand context
3. **Analyze Code Quality**: Check for:\n   - Correctness: Does the code do what the issue description asks?

Use this skill when reviewing C++ code, PRs, diffs, tests, headers, APIs, or architecture changes.

## Mission
Perform a strict senior-level review. Prioritize correctness and safety over style. Assume the code may compile yet still be wrong.

## Review order

### 1) Correctness
Look for:
- wrong logic
- bad assumptions
- edge-case failures
- invalid state transitions
- ignored failure modes
- unchecked inputs
- off-by-one errors
- invalid container access
- integer overflow / narrowing / signedness traps
- misuse of standard library APIs
- stale or invalid iterators

Questions:
- Can this produce wrong results?
- Can this crash?
- Can this silently corrupt state?
- Are error paths handled?

---

### 2) Lifetime and undefined behavior
Look for:
- dangling references
- dangling pointers
- use-after-free patterns
- returning references/views to dead objects
- storing references to temporaries
- invalid `string_view` / `span` / iterator lifetimes
- unsafe lambda captures
- invalidation after vector/map reallocation or erase
- object slicing
- uninitialized reads
- strict aliasing / reinterpret cast misuse
- null dereference risk
- double delete / manual ownership bugs

Questions:
- Who owns this object?
- Can this reference outlive its source?
- Can this container mutation invalidate something used later?
- Is there UB even if tests pass?

---

### 3) Resource management and ownership
Look for:
- raw `new/delete`
- ambiguous ownership
- incorrect smart pointer choice
- cyclic `shared_ptr`
- leaks on early return
- cleanup logic spread across code paths
- file/socket/lock/resource lifetime bugs
- custom destructors that suggest missing RAII

Questions:
- Is ownership explicit?
- Can a resource leak or be released twice?
- Would RAII remove complexity here?

Preferred direction:
- automatic storage
- RAII wrappers
- `std::unique_ptr` for exclusive ownership
- `std::shared_ptr` only with a real shared-lifetime need

---

### 4) Thread safety and concurrency
Look for:
- unsynchronized shared mutable state
- race conditions
- detached thread lifetime hazards
- unsafe access across callbacks
- lock ordering problems
- atomics used without clear reasoning
- condition variable misuse
- data published without synchronization
- reference captures crossing thread boundaries

Questions:
- Can two threads access this concurrently?
- Is the synchronization strategy clear?
- Is object lifetime valid for async work?
- Is this deterministic enough for production?

---

### 5) Exception safety
Look for:
- partial state updates on failure
- resource leaks on throw
- exception-unsafe move/copy logic
- destructors that may throw
- failure paths that leave invalid state
- low-level code depending on exceptions casually

Questions:
- What happens if construction/allocation/call fails?
- Does this provide no-throw, basic, or strong guarantee?
- Is the failure model consistent?

---

### 6) Performance
Look for:
- unnecessary copies
- missed `std::move`
- expensive pass-by-value without benefit
- repeated allocations
- no `reserve()` where obvious
- temporary object churn
- string formatting/copy overhead
- poor cache locality
- unnecessary indirection
- virtual dispatch in hot paths
- work done inside tight loops that can be hoisted

Questions:
- Is this on a hot path?
- Is there an obvious cheaper version?
- Is complexity acceptable?
- Is the code trading clarity for fake optimization?

Do not suggest micro-optimizations unless meaningful.

---

### 7) API and design
Look for:
- mixed responsibilities
- leaky abstractions
- poor separation of concerns
- misleading names
- hidden side effects
- bool/flag-heavy interfaces
- unclear units or invariants
- interfaces that make misuse easy
- over-generalized abstractions
- inheritance where composition is better

Questions:
- Is this API hard to misuse?
- Does the name reflect the behavior?
- Are preconditions and invariants clear?
- Is the abstraction paying for itself?

---

### 8) Readability and maintainability
Look for:
- long functions with mixed concerns
- deeply nested control flow
- repeated logic
- magic numbers
- confusing naming
- weak comments
- unnecessary cleverness
- hidden coupling
- poor file/namespace structure

Questions:
- Can another engineer understand this quickly?
- Is the complexity essential or accidental?
- Would this be easy to modify safely?

---

### 9) Testing
Look for:
- missing tests for core logic
- missing edge cases
- missing failure-path tests
- missing lifetime/concurrency-sensitive tests
- weak assertions
- brittle tests tied to implementation details
- no test around bug-prone parsing/state/ownership logic

Questions:
- What can break that is untested?
- Are edge cases covered?
- Are error conditions asserted?
- Are tests deterministic?

---

## Output format

Use exactly this structure:

### CRITICAL
- [issue]
  - Why it matters
  - Concrete fix

### HIGH
- [issue]
  - Why it matters
  - Concrete fix

### MEDIUM
- [issue]
  - Why it matters
  - Concrete fix

### LOW
- [issue]
  - Why it matters
  - Concrete fix

If there are no items in a section, write:
- None

---

## Review style
- Be blunt and precise.
- Focus on the highest-risk issues first.
- Prefer evidence from the code.
- Do not overpraise.
- Do not flood the review with trivial nits before addressing real risk.
- Quote small code snippets only when needed.
- Suggest fixes that a real engineer could apply immediately.

---

## Special review heuristics for C++
Pay extra attention to:
- `std::string_view`, `std::span`, iterators, references, pointers
- move-from state misuse
- capturing `this` in async callbacks
- container invalidation after mutation
- signed/unsigned comparisons
- implicit narrowing conversions
- ownership hidden in APIs
- locking scope and lock lifetime
- constructors that do too much
- destructors and exception behavior
- base classes without virtual destructors when polymorphic deletion is possible
- copying non-copy-safe or expensive objects by accident
- returning references to internal mutable state
- magic boolean parameters in APIs
- manual memory management where RAII should exist

---

## Patch review mode
If reviewing a diff:
- Focus first on newly introduced risk.
- Check whether the patch breaks old invariants.
- Check if tests actually cover the new behavior.
- Check whether a “small change” creates lifetime or ownership regressions elsewhere.

---

## Header review mode
If reviewing a header:
- Focus on API clarity, ownership, constness, dependency hygiene, exception model, and misuse resistance.

---

## Test review mode
If reviewing tests:
- Check whether the tests would catch real regressions.
- Check whether assertions are meaningful.
- Check whether important failure paths and edge cases are missing.
- Check whether the test setup hides l"""
        )
    
    @property
    def trigger_mentions_list(self) -> List[str]:
        """Get trigger mentions as a list."""
        if not self.trigger_mentions:
            return ["@DevBot", "@AI"]
        return [item.strip() for item in self.trigger_mentions.split(",") if item.strip()]
    
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

def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings

def set_current_temp_dir(temp_dir: Optional[Path]) -> None:
    global _current_temp_dir
    _current_temp_dir = temp_dir
    logger.debug(f"Current temp directory set to: {temp_dir}")

def get_current_temp_dir() -> Optional[Path]:
    return _current_temp_dir

settings = get_settings()
