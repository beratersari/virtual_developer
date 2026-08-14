"""Reporter for posting updates to JIRA."""

from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.config import settings
from src.jira.client import JiraClient, create_jira_client
from src.logger import logger
from src.state.models import JiraAgentState, TaskStatus

# Keep Jira comments readable (Server/DC plain text bodies)
_MAX_ERROR_CHARS = 1800
_MAX_SUMMARY_CHARS = 2000
_MAX_RESPONSE_CHARS = 4000


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n... (truncated)"


class JiraReporter:
    """Posts updates and reports to JIRA issues."""

    def __init__(
        self,
        client: Optional[Union[JiraClient, "SimulatedJiraClient"]] = None,
        simulated: bool = False,
    ):
        if client:
            self.client = client
        else:
            use_simulated = (
                simulated
                or not settings.is_configured()
                or settings.jira_host
                in ["", "a", "https://yourcompany.atlassian.net"]
            )
            if use_simulated:
                logger.info("Using simulated JIRA client")
            self.client = create_jira_client(simulated=use_simulated)

    def post_initial_acknowledgment(self, state: JiraAgentState) -> Optional[str]:
        """Post initial acknowledgment that the agent received the issue."""
        workflow_type = (state.metadata or {}).get("workflow_type") or "unknown"
        workflow_label = str(workflow_type).replace("_", " ").title()
        summary = (state.issue_summary or "").strip() or "(no summary)"

        body = f"""h3. AI Agent — Work Started

This issue has been accepted and will be processed automatically.

*Issue:* {state.issue_key} — {summary}
*Workflow:* {workflow_label}
*Status:* Analyzing requirements

The board status will move to *In Progress* when execution begins.
Progress updates will be posted on this issue as work proceeds.
"""
        try:
            result = self.client.add_comment(state.issue_key, body)
            return result.get("id") if result else None
        except Exception as e:
            logger.error(f"Error posting acknowledgment: {e}")
            return None

    def append_plan_to_description(self, state: JiraAgentState, plan_content: str) -> bool:
        """Append full plan to the Jira description (never overwrites prior content).

        Plan mode reports the plan on the issue itself so operators can review it
        without a GitLab push. Returns True on success.
        """
        raw = (plan_content or "").strip()
        if not raw:
            return False
        from datetime import datetime

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        block = (
            f"----\n"
            f"h3. AI Agent — Plan ({stamp})\n\n"
            f"{{code:markdown}}\n"
            f"{_clip(raw, 12000)}\n"
            f"{{code}}\n\n"
            f"_Plan file:_ {state.plan_path or 'N/A'}\n"
            f"_Mode: plan — no GitLab push. Plans never auto-start. "
            f"Open a new issue with_ `Mode: build` _to implement, or add label "
            f"`ai-start-work` / `ai-execute` on this ticket while it is To Do._\n"
        )
        try:
            if hasattr(self.client, "append_to_description"):
                return bool(self.client.append_to_description(state.issue_key, block))
            # Fallback: cannot merge safely without append helper
            logger.warning(
                f"Jira client has no append_to_description; "
                f"skipping description update for {state.issue_key}"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to append plan to description for {state.issue_key}: {e}")
            return False

    def post_plan_summary(self, state: JiraAgentState, plan_content: str) -> Optional[str]:
        """Post plan summary to JIRA (comment). Full plan is also appended to description."""
        raw = (plan_content or "").strip()
        if not raw:
            summary = (
                "_No plan content was found on disk. The planning agent may have "
                "failed to write a plan file, or the plan path does not match the "
                "workspace. Check agent session logs before starting work._"
            )
        else:
            lines = raw.split("\n")
            summary_lines = [line for line in lines[:20] if line.strip()]
            summary = "\n".join(summary_lines[:10]) or raw[:500]

        body = f"""h3. AI Agent — Plan Ready

A work plan has been generated for this issue (plan mode — *no GitLab push*).

*Summary:*
{{code:markdown}}
{summary}
{{code}}

*Plan file:* {state.plan_path or "N/A"}

The full plan has been *appended to this issue's description* (existing text preserved).

*Next steps:*
* Review the plan in the description / summary above
* *Plans never auto-start* (by design)
* To *implement*: open a *new* issue with {{noformat}}Mode: build{{noformat}} in the {{noformat}}{{params}}{{noformat}} block (and the same Repository / branches), *or* add label {{noformat}}ai-start-work{{noformat}} / {{noformat}}ai-execute{{noformat}} on this ticket while it is *To Do*
* No dashboard Start button

----
_Plan generated by the planning agent. Board poller does not read comments by default._
"""

        result = self.client.add_comment(state.issue_key, body)

        # Merge label so existing labels (e.g. ai-assist) are preserved on Server/DC
        try:
            if hasattr(self.client, "add_labels"):
                self.client.add_labels(state.issue_key, ["ai-plan-ready"])
            else:
                self.client.update_issue(state.issue_key, labels=["ai-plan-ready"])
        except Exception as e:
            logger.warning(f"Could not add plan-ready label on {state.issue_key}: {e}")

        return result.get("id") if result else None

    def post_progress_update(
        self,
        state: JiraAgentState,
        message: str,
        progress_percentage: Optional[int] = None,
    ) -> Optional[str]:
        """Post progress update."""
        if state is None:
            logger.error("Cannot post progress: state is None")
            return None

        msg = (message or "").strip() or "Progress update (no details provided)."
        msg = _clip(msg, _MAX_SUMMARY_CHARS)

        progress_line = ""
        if progress_percentage is not None:
            try:
                pct = max(0, min(100, int(progress_percentage)))
            except (TypeError, ValueError):
                pct = None
            if pct is not None:
                progress_line = f"\n*Progress:* {pct}%\n"

        body = f"""h3. AI Agent — Progress Update

{msg}{progress_line}
*Status:* {state.status.value}
"""

        try:
            result = self.client.add_comment(state.issue_key, body)
            return result.get("id") if result else None
        except Exception as e:
            logger.error(f"Error posting progress for {state.issue_key}: {e}")
            return None

    def post_completion(
        self,
        state: Optional[JiraAgentState],
        summary: str,
        changes_made: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Post completion message (no cost or token metrics)."""
        if state is None:
            logger.error("Cannot post completion: state is None")
            return None

        summary_text = (summary or "").strip() or (
            "Work finished. See the merge request / branch for details."
        )
        summary_text = _clip(summary_text, _MAX_SUMMARY_CHARS)

        changes_section = ""
        if changes_made:
            cleaned = [c.strip() for c in changes_made if c and str(c).strip()]
            if cleaned:
                changes_list = "\n".join(f"* {c}" for c in cleaned[:30])
                changes_section = f"""
*Changes made:*
{changes_list}
"""

        session_id = (
            getattr(state, "current_opencode_session_id", None)
            or getattr(state, "current_session_id", None)
            or "N/A"
        )
        completed_time = (
            state.completed_at.strftime("%Y-%m-%d %H:%M:%S")
            if state.completed_at
            else "N/A"
        )

        duration_line = ""
        if state.execution_duration_seconds is not None:
            duration_line = (
                f"*Duration:* {state.execution_duration_seconds:.1f} seconds\n"
            )

        # Delivery section — always be explicit about MR / branch outcome
        meta = state.metadata or {}
        mr_url = meta.get("merge_request_url")
        branch = meta.get("feature_branch") or meta.get("branch_name")
        delivery_status = (meta.get("delivery_status") or "").strip().lower()
        delivery_lines = ["*Delivery:*"]
        if delivery_status == "no_new_commits":
            delivery_lines.append(
                "* No new commits for this run (agent finished successfully; "
                "existing branch/MR was not re-attributed to this job)."
            )
            note = (meta.get("delivery_note") or "").strip()
            if note:
                delivery_lines.append(f"* Note: {note[:500]}")
        elif mr_url:
            delivery_lines.append(f"* Merge request: {mr_url}")
        if branch:
            delivery_lines.append(f"* Feature branch: {{noformat}}{branch}{{noformat}}")
        if delivery_status != "no_new_commits":
            if not mr_url and not branch:
                delivery_lines.append(
                    "* No merge request URL was recorded. The branch may still be on "
                    "the remote — check GitLab for {{noformat}}feature/{issue}{{noformat}}."
                    .format(issue=state.issue_key)
                )
            elif not mr_url and branch:
                delivery_lines.append(
                    "* Branch was pushed but no merge request link is available "
                    "(glab may be missing or the target branch may not exist)."
                )
        delivery_section = "\n".join(delivery_lines) + "\n"

        body = f"""h3. AI Agent — Work Completed

{summary_text}{changes_section}
{delivery_section}{duration_line}*Session:* {session_id}
*Completed at:* {completed_time}

----
_Completed by the AI agent. Please review and verify before merging or closing._
"""

        try:
            result = self.client.add_comment(state.issue_key, body)
            return result.get("id") if result else None
        except Exception as e:
            logger.error(f"Error posting completion for {state.issue_key}: {e}")
            return None

    def post_error(
        self,
        state: Optional[JiraAgentState],
        error_message: str,
        suggestion: Optional[str] = None,
        *,
        category: str = "error",
    ) -> Optional[str]:
        """Post error (or incomplete/compaction) message."""
        if state is None:
            logger.error("Cannot post error: state is None")
            return None

        err = (error_message or "").strip() or "Unknown error (no details provided)."
        err = _clip(err, _MAX_ERROR_CHARS)

        suggestion_text = (suggestion or "").strip() or (
            "Move the issue back to To Do to re-queue, or check session logs under "
            ".jira-agent/sessions/."
        )
        suggestion_section = f"\n*Suggestion:* {suggestion_text}\n"

        timeout_section = ""
        if getattr(state, "timed_out", False):
            limit = state.timeout_seconds or "unknown"
            timeout_section = f"\n*Timed out:* yes (limit {limit}s)\n"

        retry_section = ""
        if state.max_retries and state.retry_count >= state.max_retries:
            retry_section = (
                f"\n*Retries exhausted:* {state.retry_count}/{state.max_retries}\n"
            )

        session_id = state.current_opencode_session_id or "N/A"

        kind = (category or "error").strip().lower()
        if kind in {"question", "clarifying_question", "clarification"}:
            heading = "AI Agent — Clarifying question (unattended)"
            lead = (
                "The agent stopped to ask a clarifying question. This daemon "
                "runs *unattended* (one-pass prompt) — there is no human reply "
                "path. A single unattended resume nudge was tried when "
                "possible; the session is still incomplete. This is *not* a "
                "crash."
            )
        elif kind == "incomplete":
            heading = "AI Agent — Incomplete session (context compaction)"
            lead = (
                "The OpenCode session stopped after context compaction or a "
                "mid-turn idle. This is *not* a crash — the agent ran out of "
                "compact-continue budget before finishing."
            )
        else:
            heading = "AI Agent — Error"
            lead = "An error occurred while processing this issue:"

        body = f"""h3. {heading}

{lead}

{{code}}
{err}
{{code}}
{suggestion_section}{timeout_section}{retry_section}
*Status:* {state.status.value}
*Retry count:* {state.retry_count}
*Session:* {session_id}

Please review the details above and advise how to proceed (for example, move the issue back to To Do to retry).
"""

        try:
            result = self.client.add_comment(state.issue_key, body)
            return result.get("id") if result else None
        except Exception as e:
            logger.error(f"Error posting error for {state.issue_key}: {e}")
            return None

    def post_comment_response(
        self,
        issue_key: str,
        response: str,
        in_reply_to: Optional[str] = None,
    ) -> Optional[str]:
        """Post response to a comment."""
        text = (response or "").strip() or (
            "_The agent returned an empty response. Retry the @mention or check logs._"
        )
        text = _clip(text, _MAX_RESPONSE_CHARS)
        body = f"""h3. AI Agent — Response

{text}
"""

        try:
            result = self.client.add_comment(issue_key, body)
            return result.get("id") if result else None
        except Exception as e:
            logger.error(f"Error posting comment response for {issue_key}: {e}")
            return None

    def post_oracle_response(
        self,
        issue_key: str,
        question: str,
        answer: str,
    ) -> Optional[str]:
        """Post architecture consultation response."""
        q = (question or "").strip() or "(no question provided)"
        a = (answer or "").strip() or (
            "_The Oracle agent returned an empty answer. Rephrase the question or check logs._"
        )
        a = _clip(a, _MAX_RESPONSE_CHARS)
        body = f"""h3. AI Agent — Architecture Consultation

*Question:* {q}

*Answer:*
{a}

----
_Consultation provided by the Oracle agent._
"""

        try:
            result = self.client.add_comment(issue_key, body)
            return result.get("id") if result else None
        except Exception as e:
            logger.error(f"Error posting oracle response for {issue_key}: {e}")
            return None

    def update_issue_status(
        self,
        issue_key: str,
        status: str,
    ) -> bool:
        """Update issue status/transition."""
        return self.client.transition_issue(issue_key, status)

    def attach_file(
        self,
        issue_key: str,
        file_path: str,
        filename: Optional[str] = None,
    ) -> bool:
        """Attach a file to the issue."""
        result = self.client.add_attachment(issue_key, file_path, filename)
        return result is not None
