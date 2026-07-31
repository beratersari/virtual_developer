"""Build prompts for different agent types."""

from typing import Any, Dict, List, Optional

from src.config import settings


class PromptBuilder:
    """Builds prompts for various agent workflows."""

    @staticmethod
    def commit_message_block(issue_key: str) -> str:
        """Concrete commit template for this issue (agent/rules/EXECUTION.md).

        Agents commit themselves; GitManager's formatter is only a fallback.
        Injecting the filled-in issue key removes ambiguity from generic rules.
        """
        return f"""## Git Commit (MANDATORY if you changed files)

Branch: `feature/{issue_key}` (create it if needed). Do **not** push or open an MR.

**Required subject format** (from EXECUTION.md):

```text
[{issue_key}] <type>: <description>
```

Allowed types: `feat` · `fix` · `refactor` · `docs` · `test` · `perf` · `ci` · `build` · `revert` · `chore`

**Doğru format örnekleri for this issue:**
```text
[{issue_key}] feat: Yeni özellik eklendi
[{issue_key}] fix: Hata düzeltildi
[{issue_key}] refactor: Kodun çalışma şeklini değiştirmeyen iyileştirme
[{issue_key}] docs: Dökümantasyon işleri
[{issue_key}] test: Birim testler
[{issue_key}] perf: Çalışma mantığını değiştirmeyen performans iyileştirmesi
[{issue_key}] ci: CI/CD değişiklikleri
[{issue_key}] build: Build sistemi ile ilgili değişiklikler
[{issue_key}] revert: Kodu geri almak
[{issue_key}] chore: Genel işler, küçük düzeltmeler
```

```bash
git add .
git commit -m "[{issue_key}] fix: short description of the change"
```

Rules:
- Subject MUST be `[{issue_key}] type: description`
- Do not omit the type; do not use bare `feat:` without the `[{issue_key}]` prefix
- Do not push / open MR; do not commit secrets (`.env`, tokens)
"""
    
    @staticmethod
    def build_prometheus_prompt(
        issue_key: str,
        summary: str,
        description: str,
        acceptance_criteria: Optional[str] = None,
    ) -> str:
        """Build prompt for Prometheus (planning agent)."""
        
        prompt = f"""# Task Planning Request

## JIRA Issue: {issue_key}
**Summary**: {summary}

## Description
{description}

"""
        
        if acceptance_criteria:
            prompt += f"""## Acceptance Criteria
{acceptance_criteria}

"""
        
        prompt += f"## Your Task\n{settings.prompt_planning}\n"
        
        return prompt
    
    @staticmethod
    def build_atlas_prompt(
        issue_key: str,
        plan_path: str,
        previous_learnings: Optional[List[str]] = None,
    ) -> str:
        """Build prompt for Atlas (orchestrator)."""
        
        prompt = f"""# Task Execution Request

## JIRA Issue: {issue_key}

## Your Role
You are Atlas, the orchestrator. Your job is to execute the plan at:
{plan_path}

## Instructions
1. Read the plan file
2. Break down tasks and delegate to appropriate agents using the `task` tool
3. Accumulate wisdom from each subtask
4. Verify all work before marking complete
5. Update the plan file checkboxes as tasks complete

"""
        
        if previous_learnings:
            prompt += "## Previous Learnings\n"
            for learning in previous_learnings:
                prompt += f"- {learning}\n"
            prompt += "\n"
        
        prompt += f"{settings.prompt_execution}\n\n"
        prompt += PromptBuilder.commit_message_block(issue_key)
        
        return prompt
    
    @staticmethod
    def build_sisyphus_prompt(
        issue_key: str,
        task_description: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Build prompt for Sisyphus (direct execution)."""
        
        prompt = f"""# Direct Task Execution

## JIRA Issue: {issue_key}

## Task
{task_description}

"""
        
        if context:
            prompt += "## Context\n"
            if "files" in context:
                prompt += "\n**Relevant Files**:\n"
                for f in context["files"]:
                    prompt += f"- {f}\n"
            
            if "patterns" in context:
                prompt += "\n**Code Patterns**:\n"
                for p in context["patterns"]:
                    prompt += f"- {p}\n"
            
            prompt += "\n"
        
        prompt += f"{settings.prompt_direct_execution}\n\n"
        prompt += PromptBuilder.commit_message_block(issue_key)
        
        return prompt
    
    @staticmethod
    def build_comment_response_prompt(
        issue_key: str,
        comment_text: str,
        current_state: Optional[str] = None,
    ) -> str:
        """Build prompt for responding to @bot mentions."""
        
        prompt = f"""# Comment Response Request

## JIRA Issue: {issue_key}

## User Comment
{comment_text}

"""
        
        if current_state:
            prompt += f"""## Current Work State
{current_state}

"""
        
        prompt += """## Instructions
Respond to the user's request in the comment.

If they want to:
- **Start work**: Begin execution if a plan exists
- **Check status**: Report current progress
- **Make changes**: Implement the specific request
- **Ask question**: Provide a clear answer

Be concise and actionable in your response.
"""
        
        return prompt

    @staticmethod
    def build_oracle_consult_prompt(
        question: str,
        context_files: Optional[List[str]] = None,
    ) -> str:
        """Build prompt for Oracle (architecture consultation)."""
        
        prompt = f"""# Architecture Consultation

## Question
{question}

## Your Role
You are Oracle. Provide expert architecture guidance.

"""
        
        if context_files:
            prompt += "## Context Files\n"
            for f in context_files:
                prompt += f"- {f}\n"
            prompt += "\n"
        
        prompt += f"{settings.prompt_oracle}\n"
        
        return prompt
