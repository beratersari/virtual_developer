"""Build prompts for different agent types."""

from typing import Any, Dict, List, Optional

from src.config import settings


class PromptBuilder:
    """Builds prompts for various agent workflows."""
    
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
        
        prompt += f"{settings.prompt_execution}\n"
        
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
        
        prompt += f"{settings.prompt_direct_execution}\n"
        
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
    def build_code_review_prompt(
        issue_key: str,
        summary: str,
        description: str,
        review_model: str,
    ) -> str:
        """Build prompt for code review after successful execution.
        
        The review agent is expected to read files and git diff output,
        but NOT make any edits.  The body of the review instructions comes
        from ``settings.prompt_code_review`` so it can be customised via .env.
        """
        prompt = f"""# Code Review Request

## JIRA Issue: {issue_key}
**Summary**: {summary}

## Original Task Description
{description}

## Review Model
You are running as a code reviewer using model: {review_model}

## Your Task
{settings.prompt_code_review}
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
