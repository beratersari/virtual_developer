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
        
        prompt += """## Your Task
As Prometheus, create a comprehensive work plan for this JIRA issue.

1. **Interview Mode**: Ask clarifying questions if requirements are ambiguous
2. **Research**: Explore the codebase to understand existing patterns
3. **Plan Generation**: Create a detailed plan with:
   - Task breakdown with checkboxes
   - File references and locations
   - Implementation approach
   - Testing strategy
   - Estimated effort

Output the plan to the designated plan file.
"""
        
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
        
        prompt += """## Delegation Guidelines
- Use `category="visual-engineering"` for UI/UX work
- Use `category="deep"` for complex problem-solving
- Use `category="quick"` for simple fixes
- Use `subagent_type="oracle"` for architecture decisions
- Use `subagent_type="explore"` for codebase research

## Success Criteria
- All plan checkboxes checked
- Tests passing
- No type errors
- Code follows project conventions
"""
        
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
        
        prompt += """## Instructions
1. Analyze the task and current codebase
2. Create todos for multi-step work
3. Implement the solution following existing patterns
4. Run verification (tests, type checking)
5. Report completion with summary of changes

## Constraints
- Follow existing code style
- Add tests for new functionality
- Do not break existing tests
- Minimal, focused changes
"""
        
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
        
        prompt += """## Response Format
1. **Direct Answer**: Clear response to the question
2. **Rationale**: Why this approach is recommended
3. **Alternatives**: Other options considered
4. **Trade-offs**: Pros/cons of each approach
5. **Implementation Hints**: Key files/patterns to use

Be thorough but concise. Focus on practical guidance.
"""
        
        return prompt
