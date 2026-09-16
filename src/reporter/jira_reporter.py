"""Reporter for posting updates to JIRA."""

from typing import List, Optional, Union

from src.config import settings
from src.jira.client import JiraClient, create_jira_client
from src.logger import logger
from src.state.models import JiraAgentState

# Keep Jira comments readable (Server/DC plain text bodies)
_MAX_ERROR_CHARS = 1800
_MAX_SUMMARY_CHARS = 2000
_MAX_RESPONSE_CHARS = 8000
_MAX_ANSWER_CHARS = 8000


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n... (truncated)"


def _header_for_state(kind: str, state: Optional[JiraAgentState]) -> str:
    from src.brand import format_reply_header, resolve_reply_ids

    model, job_id = resolve_reply_ids(state)
    return format_reply_header(kind, model=model, job_id=job_id)


def _human_agent_answer(text: str) -> str:
    """Codex JSONL → last assistant markdown. OpenCode serve log lines stripped."""
    from src.backends.codex import format_agent_answer_for_comment

    out = format_agent_answer_for_comment(text or "")
    return "" if out == "(no output)" else out


class JiraReporter:
    """Posts updates and reports to JIRA issues (or Azure work items)."""

    def _issue_client(
        self, issue_key: str, state: Optional[JiraAgentState] = None
    ):
        from src.azure.tracker import azure_tracker_for

        tracker = azure_tracker_for(issue_key, state)
        return tracker or self.client

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
        from src.operator_copy import (
            ACK_ANALYZING,
            ACK_BOARD,
            ACK_HEADING,
            ACK_INTRO,
            ACK_ISSUE,
            ACK_STATUS,
            ACK_WORKFLOW,
            NO_SUMMARY,
            workflow_label as _wf_label,
        )

        workflow_label = _wf_label((state.metadata or {}).get("workflow_type"))
        summary = (state.issue_summary or "").strip() or NO_SUMMARY
        workflow_line = f"\n*{ACK_WORKFLOW}:* {workflow_label}" if workflow_label else ""

        body = f"""{_header_for_state("Work started", state)}

h3. {ACK_HEADING}

{ACK_INTRO}

*{ACK_ISSUE}:* {state.issue_key} — {summary}{workflow_line}
*{ACK_STATUS}:* {ACK_ANALYZING}

{ACK_BOARD}
"""
        try:
            result = self._issue_client(state.issue_key, state).add_comment(
                state.issue_key, body
            )
            return result.get("id") if result else None
        except Exception as e:
            logger.error(f"Error posting acknowledgment: {e}")
            return None

    def append_plan_to_description(self, state: JiraAgentState, plan_content: str) -> bool:
        """Deprecated no-op. Plans are posted as comments only."""
        key = getattr(state, "issue_key", None) or "?"
        logger.info(
            f"Skipping Jira description append for {key}; plan is comment-only"
        )
        return False

    def post_plan_summary(self, state: JiraAgentState, plan_content: str) -> Optional[str]:
        """Post the full plan as a Jira comment. Do not write it on the description."""
        from src.operator_copy import (
            PLAN_EMPTY,
            PLAN_FILE,
            PLAN_FOOTER,
            PLAN_HEADING,
            PLAN_INTRO,
            PLAN_LABEL,
            PLAN_NEXT,
            PLAN_NEXT_AZURE,
            PLAN_NEXT_JIRA,
        )

        raw = (plan_content or "").strip()
        if not raw:
            plan_block = PLAN_EMPTY
        else:
            plan_block = (
                "{{code:markdown}}\n"
                f"{_clip(raw, 12000)}\n"
                "{{code}}"
            )

        from src.azure.keys import is_azure_work_item_key

        if is_azure_work_item_key(getattr(state, "issue_key", "") or ""):
            next_steps = PLAN_NEXT_AZURE
        else:
            next_steps = PLAN_NEXT_JIRA

        body = f"""{_header_for_state("Plan", state)}

h3. {PLAN_HEADING}

{PLAN_INTRO}

*{PLAN_LABEL}:*
{plan_block}

*{PLAN_FILE}:* {state.plan_path or "N/A"}

*{PLAN_NEXT}:*
{next_steps}

----
{PLAN_FOOTER}
"""

        client = self._issue_client(state.issue_key, state)
        result = client.add_comment(state.issue_key, body)
        try:
            from src.jira.plan_labels import PLAN_READY_LABEL

            if not is_azure_work_item_key(state.issue_key) and hasattr(
                client, "add_labels"
            ):
                client.add_labels(state.issue_key, [PLAN_READY_LABEL])
        except Exception as e:
            logger.warning(f"Could not add plan_ready label on {state.issue_key}: {e}")
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

        from src.operator_copy import (
            ACK_STATUS,
            PROGRESS_EMPTY,
            PROGRESS_HEADING,
            PROGRESS_PCT,
        )

        msg = (message or "").strip() or PROGRESS_EMPTY
        msg = _clip(msg, _MAX_SUMMARY_CHARS)

        progress_line = ""
        if progress_percentage is not None:
            try:
                pct = max(0, min(100, int(progress_percentage)))
            except (TypeError, ValueError):
                pct = None
            if pct is not None:
                progress_line = f"\n*{PROGRESS_PCT}:* {pct}%\n"

        body = f"""{_header_for_state("Progress", state)}

h3. {PROGRESS_HEADING}

{msg}{progress_line}
*{ACK_STATUS}:* {state.status.value}
"""

        try:
            result = self._issue_client(state.issue_key, state).add_comment(
                state.issue_key, body
            )
            return result.get("id") if result else None
        except Exception as e:
            logger.error(f"Error posting progress for {state.issue_key}: {e}")
            return None

    def post_completion(
        self,
        state: Optional[JiraAgentState],
        summary: str,
        changes_made: Optional[List[str]] = None,
        *,
        agent_answer: Optional[str] = None,
    ) -> Optional[str]:
        """Post completion message (no cost or token metrics)."""
        if state is None:
            logger.error("Cannot post completion: state is None")
            return None

        from src.operator_copy import (
            DONE_AT,
            DONE_BRANCH,
            DONE_CHANGES,
            DONE_DELIVERY,
            DONE_DURATION,
            DONE_FOOTER,
            DONE_GENERIC,
            DONE_HEADING,
            DONE_MR,
            DONE_NO_COMMITS,
            DONE_NO_MR,
            DONE_NOTE,
            DONE_PUSH_NO_MR,
            DONE_SECONDS,
            DONE_SESSION,
        )

        answer_text = _human_agent_answer(agent_answer or "")
        generic = {
            "",
            "All tasks completed successfully.",
            "Work finished. See the merge request / branch for details.",
            DONE_GENERIC,
        }
        summary_raw = (summary or "").strip()
        if answer_text and summary_raw in generic:
            summary_text = _clip(answer_text, _MAX_ANSWER_CHARS)
        elif answer_text:
            summary_text = _clip(
                f"{summary_raw}\n\n{answer_text}",
                _MAX_ANSWER_CHARS,
            )
        else:
            summary_text = summary_raw or DONE_GENERIC
            summary_text = _clip(summary_text, _MAX_SUMMARY_CHARS)

        changes_section = ""
        if changes_made:
            cleaned = [c.strip() for c in changes_made if c and str(c).strip()]
            if cleaned:
                changes_list = "\n".join(f"* {c}" for c in cleaned[:30])
                changes_section = f"""
*{DONE_CHANGES}:*
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
                f"*{DONE_DURATION}:* {state.execution_duration_seconds:.1f} "
                f"{DONE_SECONDS}\n"
            )

        # Delivery section — always be explicit about MR / branch outcome
        meta = state.metadata or {}
        mr_url = meta.get("merge_request_url")
        branch = meta.get("feature_branch") or meta.get("branch_name")
        delivery_status = (meta.get("delivery_status") or "").strip().lower()
        delivery_lines = [f"*{DONE_DELIVERY}:*"]
        if delivery_status == "no_new_commits":
            delivery_lines.append(DONE_NO_COMMITS)
            note = (meta.get("delivery_note") or "").strip()
            if note:
                delivery_lines.append(f"* {DONE_NOTE}: {note[:500]}")
        elif mr_url:
            delivery_lines.append(f"* {DONE_MR}: {mr_url}")
        if branch:
            delivery_lines.append(f"* {DONE_BRANCH}: {{noformat}}{branch}{{noformat}}")
        if delivery_status != "no_new_commits":
            if not mr_url and not branch:
                delivery_lines.append(DONE_NO_MR.format(issue=state.issue_key))
            elif not mr_url and branch:
                delivery_lines.append(DONE_PUSH_NO_MR)
        delivery_section = "\n".join(delivery_lines) + "\n"

        body = f"""{_header_for_state("Done", state)}

h3. {DONE_HEADING}

{summary_text}{changes_section}
{delivery_section}{duration_line}*{DONE_SESSION}:* {session_id}
*{DONE_AT}:* {completed_time}

----
{DONE_FOOTER}
"""

        try:
            result = self._issue_client(state.issue_key, state).add_comment(
                state.issue_key, body
            )
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

        from src.operator_copy import (
            ACK_STATUS,
            DONE_SESSION,
            FAILED_DEFAULT_SUGGEST,
            FAILED_EMPTY,
            FAILED_FOOTER,
            FAILED_RETRIES,
            FAILED_RETRY_COUNT,
            FAILED_SUGGESTION,
            FAILED_TIMEOUT,
            FAILED_TIMEOUT_YES,
            fail_category,
        )

        err = (error_message or "").strip() or FAILED_EMPTY
        err = _clip(err, _MAX_ERROR_CHARS)

        suggestion_text = (suggestion or "").strip() or FAILED_DEFAULT_SUGGEST
        suggestion_section = f"\n*{FAILED_SUGGESTION}:* {suggestion_text}\n"

        timeout_section = ""
        if getattr(state, "timed_out", False):
            limit = state.timeout_seconds or "—"
            timeout_section = f"\n*{FAILED_TIMEOUT}:* {FAILED_TIMEOUT_YES.format(limit=limit)}\n"

        retry_section = ""
        if state.max_retries and state.retry_count >= state.max_retries:
            retry_section = (
                f"\n*{FAILED_RETRIES}:* {state.retry_count}/{state.max_retries}\n"
            )

        session_id = state.current_opencode_session_id or "N/A"
        heading, lead = fail_category(category)

        body = f"""{_header_for_state("Failed", state)}

h3. {heading}

{lead}

{{code}}
{err}
{{code}}
{suggestion_section}{timeout_section}{retry_section}
*{ACK_STATUS}:* {state.status.value}
*{FAILED_RETRY_COUNT}:* {state.retry_count}
*{DONE_SESSION}:* {session_id}

{FAILED_FOOTER}
"""

        try:
            result = self._issue_client(state.issue_key, state).add_comment(
                state.issue_key, body
            )
            return result.get("id") if result else None
        except Exception as e:
            logger.error(f"Error posting error for {state.issue_key}: {e}")
            return None

    def post_comment_response(
        self,
        issue_key: str,
        response: str,
    ) -> Optional[str]:
        """Post response to a comment."""
        from src.operator_copy import ANSWER_EMPTY, ANSWER_HEADING

        text = _human_agent_answer(response)
        text = (text or "").strip() or ANSWER_EMPTY
        text = _clip(text, _MAX_RESPONSE_CHARS)
        body = f"""{_header_for_state("Answer", None)}

h3. {ANSWER_HEADING}

{text}
"""

        try:
            result = self._issue_client(issue_key).add_comment(issue_key, body)
            return result.get("id") if result else None
        except Exception as e:
            logger.error(f"Error posting comment response for {issue_key}: {e}")
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
