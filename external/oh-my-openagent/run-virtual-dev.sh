#!/bin/bash
# run-virtual-dev.sh
# Run oh-my-opencode virtual developer against any project
#
# Usage:
#   ./run-virtual-dev.sh "Read Jira ticket PROJ-123. Fix the bug. Create branch. Commit. Push."
#   ./run-virtual-dev.sh /path/to/project "Fix the login bug"
#
# Requirements:
#   - Docker installed
#   - (Optional) API keys in .env file for better models

set -e

# Parse arguments
if [ -d "$1" ]; then
  TARGET_PROJECT="$1"
  TASK="${2:-Your task: Read Jira ticket. Implement fix. Create branch. Commit. Push.}"
else
  TARGET_PROJECT="$(pwd)"
  TASK="${1:-Your task: Read Jira ticket. Implement fix. Create branch. Commit. Push.}"
fi

echo "Target project: $TARGET_PROJECT"
echo "Task: $TASK"
echo ""

# Build the image if needed
if ! docker image inspect ohmyopencode-virtual-dev &>/dev/null; then
  echo "Building Docker image..."
  docker build -f Dockerfile.virtual-dev -t ohmyopencode-virtual-dev .
fi

# Run the agent
docker run --rm \
  -v "$TARGET_PROJECT:/workspace" \
  -w /workspace \
  -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
  -e OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
  -e GITHUB_TOKEN="${GITHUB_TOKEN:-}" \
  ohmyopencode-virtual-dev \
  "$TASK"
