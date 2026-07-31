"""FastAPI webhook server for JIRA events."""

import hashlib
import hmac
import json
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Request

from src.config import settings
from src.logger import logger

# Event handler type
EventHandler = Callable[[Dict[str, Any]], None]


def verify_webhook_signature(
    body: bytes,
    signature: Optional[str],
    webhook_secret: Optional[str],
) -> bool:
    """Verify JIRA webhook signature if secret is configured."""
    if not webhook_secret:
        return True
    if signature is None or signature == "":
        return False

    expected = hmac.new(
        webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


def create_webhook_app(
    on_issue_created: Optional[EventHandler] = None,
    on_issue_updated: Optional[EventHandler] = None,
    on_comment_added: Optional[EventHandler] = None,
    secret: Optional[str] = None,
) -> FastAPI:
    """Create FastAPI app for handling JIRA webhooks."""
    
    app = FastAPI(title="JIRA Virtual Developer Webhook")
    webhook_secret = secret or settings.webhook_secret
    
    def verify_signature(body: bytes, signature: Optional[str]) -> bool:
        return verify_webhook_signature(body, signature, webhook_secret)
    
    def should_process_issue(issue_data: Dict[str, Any]) -> bool:
        """Check if issue should be processed based on filters."""
        fields = issue_data.get("fields", {})
        
        # Check project (skip if no project filter configured or no project in issue)
        project = fields.get("project", {}).get("key", "")
        if project and settings.jira_projects_list and settings.jira_projects_list != ['']:
            if project not in settings.jira_projects_list:
                logger.info(f"Ignoring issue: project '{project}' not in {settings.jira_projects_list}")
                return False

        # Check labels
        labels = fields.get("labels", [])
        if any(label in settings.trigger_labels_list for label in labels):
            logger.info(f"Processing issue: matched label in {labels}")
            return True

        # Check assignee
        if settings.trigger_on_assignment:
            assignee = fields.get("assignee")
            if assignee:
                logger.info(f"Processing issue: has assignee {assignee}")
                return True

        logger.info(f"Ignoring issue: no matching labels or assignee trigger")
        return False
    
    def contains_trigger_mention(body: str) -> bool:
        """Check if comment/issue body contains bot mention."""
        body_lower = body.lower()
        return any(mention.lower() in body_lower for mention in settings.trigger_mentions_list)
    
    @app.post(settings.webhook_path)
    async def handle_webhook(
        request: Request,
        x_hub_signature: Optional[str] = Header(None),
    ):
        """Handle incoming JIRA webhook."""
        body = await request.body()

        logger.info(f"Received request to {settings.webhook_path}")

        # Verify signature if configured
        if webhook_secret and not verify_signature(body, x_hub_signature):
            logger.warning(f"Invalid signature")
            raise HTTPException(status_code=401, detail="Invalid signature")

        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON")
            raise HTTPException(status_code=400, detail="Invalid JSON")

        event_type = event.get("webhookEvent", "")
        issue_key = event.get("issue", {}).get("key", "unknown")

        logger.info(f"Event: {event_type}, Issue: {issue_key}")
        
        # Handle issue created
        if event_type == "jira:issue_created":
            issue = event.get("issue", {})
            logger.debug(f"Checking if should process issue {issue_key}")
            if should_process_issue(issue):
                logger.info(f"Processing issue {issue_key}")
                if on_issue_created:
                    on_issue_created(event)
                return {"status": "processed", "event": "issue_created", "issue": issue_key}
            else:
                logger.info(f"Issue {issue_key} ignored by filters")
        
        # Handle issue updated (labels, assignee)
        elif event_type == "jira:issue_updated":
            issue = event.get("issue", {})
            changelog = event.get("changelog", {})
            
            # Check if relevant fields changed
            items = changelog.get("items", [])
            relevant_changes = {"labels", "assignee"}
            
            if any(item.get("field") in relevant_changes for item in items):
                if should_process_issue(issue):
                    logger.info(f"Processing issue update {issue_key}")
                    if on_issue_updated:
                        on_issue_updated(event)
                    return {"status": "processed", "event": "issue_updated", "issue": issue_key}
        
        # Handle comment added
        elif event_type == "comment_created":
            comment = event.get("comment", {})
            body_text = comment.get("body", "")
            
            if contains_trigger_mention(body_text):
                logger.info(f"Processing comment on {issue_key}")
                if on_comment_added:
                    on_comment_added(event)
                return {"status": "processed", "event": "comment_added", "issue": issue_key}

        logger.debug(f"Event ignored: {event_type}")
        return {"status": "ignored", "event": event_type, "issue": issue_key}
    
    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        return {"status": "healthy"}
    
    return app
