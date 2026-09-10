"""Canonical webhook URLs shown in Settings and docs.

Legacy ``/webhooks/{gitlab,azure}`` stay registered so existing hooks
keep working.
"""

GITLAB_WEBHOOK_PATH = "/yaver/webhook/gitlab"
AZURE_WEBHOOK_PATH = "/yaver/webhook/azure"

GITLAB_WEBHOOK_ALIASES = (GITLAB_WEBHOOK_PATH, "/webhooks/gitlab")
AZURE_WEBHOOK_ALIASES = (AZURE_WEBHOOK_PATH, "/webhooks/azure")

ALL_WEBHOOK_PATHS = GITLAB_WEBHOOK_ALIASES + AZURE_WEBHOOK_ALIASES
