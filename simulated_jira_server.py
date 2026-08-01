#!/usr/bin/env python3
"""
Simulated JIRA Server for Testing

In-memory REST API for local issue create/list/comment flows.
Does not push events to the daemon — production intake is the board poller.
Use ``cli.py process`` (or a real board + poller) to run agent work.
"""

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)


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
            },
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
        key: Optional[str] = None,
    ) -> Issue:
        """Create a new issue."""
        if key is None:
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


# Flask Routes
@app.route("/")
def index():
    return jsonify({
        "service": "Simulated JIRA Server",
        "version": "1.0.0",
        "issues_count": len(store.issues),
        "intake": "poller-only (no webhook push)",
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
        key=data.get("key"),  # Allow manual key override
    )

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

    return jsonify({"success": True, "comment": asdict(comment)}), 201


@app.route("/api/issues/<key>/assign", methods=["POST"])
def assign_issue(key):
    """Assign an issue to someone."""
    data = request.json
    issue = store.update_issue(key, assignee=data.get("assignee"))
    if not issue:
        return jsonify({"error": "Issue not found"}), 404

    return jsonify({"success": True, "issue": issue.to_simple_dict()})


def run_server(host="0.0.0.0", port=7001, debug=False):
    """Run the simulated JIRA server."""
    print("=" * 60)
    print("Simulated JIRA Server")
    print("=" * 60)
    print(f"Server: http://{host}:{port}")
    print("API Endpoints:")
    print("  GET  /api/issues              - List all issues")
    print("  POST /api/issues              - Create new issue")
    print("  GET  /api/issues/<key>        - Get issue details")
    print("  PUT  /api/issues/<key>        - Update issue")
    print("  POST /api/issues/<key>/comments - Add comment")
    print("  POST /api/issues/<key>/assign - Assign issue")
    print("=" * 60)
    print("Note: no webhook push — use board poller or cli.py process")
    print("=" * 60)

    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7001
    run_server(port=port)
