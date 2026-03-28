#!/usr/bin/env python3
"""
Simulated JIRA Server for Testing

This server simulates JIRA's webhook system and REST API for local testing.
It stores issues in memory and triggers webhooks to the JIRA Virtual Developer.
"""

import json
import threading
import httpx
import hashlib
import hmac
from datetime import datetime
from typing import Dict, List, Optional
from pathlib import Path
from dataclasses import dataclass, field, asdict
from flask import Flask, request, jsonify
from flask_cors import CORS
import uuid

app = Flask(__name__)
CORS(app)

# In-memory storage
@dataclass
class Comment:
    id: str
    body: str
    author: str
    created: str
    updated: str

@dataclass
class Issue:
    key: str
    summary: str
    description: str
    status: str
    issue_type: str
    priority: str
    assignee: Optional[str]
    reporter: str
    labels: List[str] = field(default_factory=list)
    comments: List[Comment] = field(default_factory=list)
    created: str = field(default_factory=lambda: datetime.now().isoformat())
    updated: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> dict:
        """Convert to JIRA-compatible format with fields wrapper."""
        return {
            "key": self.key,
            "fields": {
                "summary": self.summary,
                "description": self.description,
                "status": {"name": self.status},
                "issuetype": {"name": self.issue_type},
                "priority": {"name": self.priority},
                "assignee": {"displayName": self.assignee} if self.assignee else None,
                "reporter": {"displayName": self.reporter},
                "labels": self.labels,
                "comment": {"comments": [asdict(c) for c in self.comments]},
                "created": self.created,
                "updated": self.updated,
            }
        }
    
    def to_simple_dict(self) -> dict:
        """Convert to simple flat format for API responses."""
        return {
            "key": self.key,
            "summary": self.summary,
            "description": self.description,
            "status": self.status,
            "issue_type": self.issue_type,
            "priority": self.priority,
            "assignee": self.assignee,
            "reporter": self.reporter,
            "labels": self.labels,
            "comments": [asdict(c) for c in self.comments],
            "created": self.created,
            "updated": self.updated,
        }

class SimulatedJiraStore:
    def __init__(self):
        self.issues: Dict[str, Issue] = {}
        self.webhook_url: Optional[str] = None
        self.webhook_secret: Optional[str] = None
        self.issue_counter = 1000
        
    def create_issue(
        self,
        summary: str,
        description: str,
        issue_type: str = "Task",
        priority: str = "Medium",
        assignee: Optional[str] = None,
        reporter: str = "admin",
        labels: Optional[List[str]] = None,
    ) -> Issue:
        """Create a new issue."""
        self.issue_counter += 1
        key = f"SIM-{self.issue_counter}"
        
        issue = Issue(
            key=key,
            summary=summary,
            description=description,
            status="To Do",
            issue_type=issue_type,
            priority=priority,
            assignee=assignee,
            reporter=reporter,
            labels=labels or [],
        )
        self.issues[key] = issue
        return issue
    
    def get_issue(self, key: str) -> Optional[Issue]:
        return self.issues.get(key)
    
    def update_issue(self, key: str, **kwargs) -> Optional[Issue]:
        issue = self.issues.get(key)
        if not issue:
            return None
        
        for key_name, value in kwargs.items():
            if hasattr(issue, key_name):
                setattr(issue, key_name, value)
        
        issue.updated = datetime.now().isoformat()
        return issue
    
    def add_comment(self, key: str, body: str, author: str = "admin") -> Optional[Comment]:
        issue = self.issues.get(key)
        if not issue:
            return None
        
        comment = Comment(
            id=str(uuid.uuid4()),
            body=body,
            author=author,
            created=datetime.now().isoformat(),
            updated=datetime.now().isoformat(),
        )
        issue.comments.append(comment)
        issue.updated = datetime.now().isoformat()
        return comment
    
    def list_issues(self) -> List[Issue]:
        return list(self.issues.values())

# Global store
store = SimulatedJiraStore()

def trigger_webhook(event_type: str, issue: Issue, comment: Optional[Comment] = None):
    """Trigger webhook to the JIRA Virtual Developer (synchronous)."""
    if not store.webhook_url:
        print(f"[Webhook] No webhook URL configured, skipping {event_type}")
        return
    
    webhook_payload = {
        "webhookEvent": event_type,
        "timestamp": datetime.now().isoformat(),
        "issue": issue.to_dict(),
    }
    
    if comment:
        webhook_payload["comment"] = asdict(comment)
    
    def send_webhook():
        """Send webhook in background thread."""
        try:
            # Prepare payload bytes for signature
            payload_bytes = json.dumps(webhook_payload).encode('utf-8')
            
            # Build headers
            headers = {
                "Content-Type": "application/json",
            }
            
            # Add signature if webhook secret is configured
            if store.webhook_secret:
                signature = hmac.new(
                    store.webhook_secret.encode(),
                    payload_bytes,
                    hashlib.sha256,
                ).hexdigest()
                headers["x-hub-signature"] = f"sha256={signature}"
                print(f"[Webhook] Sending with signature: sha256={signature[:16]}...")
            
            with httpx.Client() as client:
                response = client.post(
                    store.webhook_url,
                    content=payload_bytes,
                    headers=headers,
                    timeout=30.0,
                )
                print(f"[Webhook] {event_type} -> {store.webhook_url}: {response.status_code}")
                if response.status_code != 200:
                    print(f"[Webhook] Response: {response.text}")
        except Exception as e:
            print(f"[Webhook] Error triggering webhook: {e}")
    
    # Run webhook in background thread to not block the response
    thread = threading.Thread(target=send_webhook)
    thread.daemon = True
    thread.start()

# Flask Routes
@app.route("/")
def index():
    return jsonify({
        "service": "Simulated JIRA Server",
        "version": "1.0.0",
        "issues_count": len(store.issues),
        "webhook_url": store.webhook_url,
    })

@app.route("/api/issues", methods=["GET"])
def list_issues():
    """List all issues."""
    issues = [issue.to_simple_dict() for issue in store.list_issues()]
    return jsonify({"issues": issues, "total": len(issues)})

@app.route("/api/issues", methods=["POST"])
def create_issue():
    """Create a new issue."""
    data = request.json
    
    issue = store.create_issue(
        summary=data.get("summary", "No summary"),
        description=data.get("description", ""),
        issue_type=data.get("issue_type", "Task"),
        priority=data.get("priority", "Medium"),
        assignee=data.get("assignee"),
        reporter=data.get("reporter", "admin"),
        labels=data.get("labels", []),
    )
    
    # Trigger webhook in background (uses JIRA format)
    trigger_webhook("jira:issue_created", issue)
    
    return jsonify({"success": True, "issue": issue.to_simple_dict()}), 201

@app.route("/api/issues/<key>", methods=["GET"])
def get_issue(key):
    """Get a specific issue."""
    issue = store.get_issue(key)
    if not issue:
        return jsonify({"error": "Issue not found"}), 404
    return jsonify(issue.to_simple_dict())

@app.route("/api/issues/<key>", methods=["PUT"])
def update_issue(key):
    """Update an issue."""
    data = request.json
    issue = store.update_issue(key, **data)
    if not issue:
        return jsonify({"error": "Issue not found"}), 404
    
    # Trigger webhook in background (uses JIRA format)
    trigger_webhook("jira:issue_updated", issue)
    
    return jsonify({"success": True, "issue": issue.to_simple_dict()})

@app.route("/api/issues/<key>/comments", methods=["POST"])
def add_comment(key):
    """Add a comment to an issue."""
    data = request.json
    issue = store.get_issue(key)
    if not issue:
        return jsonify({"error": "Issue not found"}), 404
    
    comment = store.add_comment(
        key,
        body=data.get("body", ""),
        author=data.get("author", "admin"),
    )
    
    # Trigger webhook in background
    trigger_webhook("jira:issue_commented", issue, comment)
    
    return jsonify({"success": True, "comment": asdict(comment)}), 201

@app.route("/api/issues/<key>/assign", methods=["POST"])
def assign_issue(key):
    """Assign an issue to someone."""
    data = request.json
    issue = store.update_issue(key, assignee=data.get("assignee"))
    if not issue:
        return jsonify({"error": "Issue not found"}), 404
    
    # Trigger webhook in background (uses JIRA format)
    trigger_webhook("jira:issue_assigned", issue)
    
    return jsonify({"success": True, "issue": issue.to_simple_dict()})

@app.route("/api/webhook", methods=["POST"])
def register_webhook():
    """Register webhook URL."""
    data = request.json
    store.webhook_url = data.get("webhook_url")
    print(f"[Server] Webhook registered: {store.webhook_url}")
    return jsonify({"success": True, "webhook_url": store.webhook_url})

@app.route("/api/webhook", methods=["GET"])
def get_webhook():
    """Get current webhook URL."""
    return jsonify({"webhook_url": store.webhook_url})

@app.route("/api/notify", methods=["POST"])
def notify_bot():
    """Manual API to notify the bot about an issue."""
    data = request.json
    key = data.get("issue_key")
    
    if key:
        issue = store.get_issue(key)
        if not issue:
            return jsonify({"error": "Issue not found"}), 404
    else:
        # Create issue if not exists
        issue = store.create_issue(
            summary=data.get("summary", "Test issue"),
            description=data.get("description", "Test description"),
            issue_type=data.get("issue_type", "Task"),
            priority=data.get("priority", "Medium"),
            assignee=data.get("assignee", "DevBot"),
            labels=data.get("labels", ["ai-assist"]),
        )
        key = issue.key
    
    event_type = data.get("event_type", "jira:issue_created")
    
    # Trigger webhook in background
    trigger_webhook(event_type, issue)
    
    return jsonify({
        "success": True,
        "message": f"Notification sent for {key}",
        "issue": issue.to_simple_dict(),
    })

def run_server(host="0.0.0.0", port=7001, debug=False):
    """Run the simulated JIRA server."""
    import os
    
    print(f"=" * 60)
    print(f"Simulated JIRA Server")
    print(f"=" * 60)
    print(f"Server: http://{host}:{port}")
    print(f"API Endpoints:")
    print(f"  GET  /api/issues              - List all issues")
    print(f"  POST /api/issues              - Create new issue")
    print(f"  GET  /api/issues/<key>        - Get issue details")
    print(f"  PUT  /api/issues/<key>        - Update issue")
    print(f"  POST /api/issues/<key>/comments - Add comment")
    print(f"  POST /api/issues/<key>/assign - Assign issue")
    print(f"  POST /api/webhook             - Register webhook URL")
    print(f"  POST /api/notify              - Notify bot (manual trigger)")
    print(f"=" * 60)
    
    # Set default webhook to local JIRA Virtual Developer
    store.webhook_url = os.environ.get(
        "SIMULATED_JIRA_WEBHOOK_URL", 
        "http://localhost:7000/webhook/jira"
    )
    
    # Set webhook secret from environment (must match the target server's secret)
    store.webhook_secret = os.environ.get(
        "WEBHOOK_SECRET",
        "dev-secret-key"  # Default from .env
    )
    
    print(f"Webhook URL: {store.webhook_url}")
    print(f"Webhook Secret: {'Yes (sending signatures)' if store.webhook_secret else 'No (unsigned)'}")
    print(f"=" * 60)
    
    app.run(host=host, port=port, debug=debug)

if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7001
    run_server(port=port)
