# JIRA Virtual Developer

A Python-based integration between **JIRA** and **Oh My OpenAgent** that enables AI agents to work on JIRA issues automatically.

## Overview

This daemon listens for JIRA events (via webhooks or polling) and triggers Oh My OpenAgent workflows to:

1. **Plan** complex tasks using Prometheus
2. **Execute** plans using Atlas orchestration
3. **Respond** to @mentions in comments
4. **Consult** on architecture using Oracle

## Table of Contents

- [How It Works with Real JIRA](#how-it-works-with-real-jira)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Real JIRA Server Setup](#real-jira-server-setup)
- [Webhook Configuration](#webhook-configuration)
- [Testing Without JIRA](#testing-without-jira)
- [CLI Commands](#cli-commands)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)

## How It Works with Real JIRA

When integrated with your actual JIRA server, the system works as follows:

### Workflow

1. **Issue Created** in JIRA → JIRA sends webhook to your server
2. **Webhook Received** → Bot validates signature and extracts issue data
3. **Workflow Detection** → Bot determines if issue needs planning or direct execution
4. **Agent Execution** → Oh My OpenAgent runs in background with appropriate agent
5. **Progress Updates** → Bot posts comments to JIRA with status and progress
6. **Completion** → Results posted to JIRA with cost summary and session details

### Real JIRA Integration Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           YOUR JIRA SERVER                               │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────────────┐  │
│  │   Issue     │    │   Comment   │    │         Webhook             │  │
│  │  Created    │───▶│   Added     │───▶│   http://your-server:7000   │  │
│  └─────────────┘    └─────────────┘    └─────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     JIRA VIRTUAL DEVELOPER (Port 7000)                   │
│  ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐   │
│  │  Webhook Server  │───▶│  Job Processor   │───▶│  Agent Runner    │   │
│  │  (FastAPI)       │    │  (Workflow)      │    │  (Oh My OpenCode)│   │
│  └──────────────────┘    └──────────────────┘    └──────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
                              ┌─────────────────────┐
                              │   YOUR CODEBASE     │
                              │   (PROJECT_ROOT)    │
                              └─────────────────────┘
```

### What Happens When You Plug Into Your JIRA Server

**Yes, it will work with your JIRA server** when properly configured:

1. **Webhook Events**: JIRA will send HTTP POST requests to your configured webhook URL whenever issues are created, updated, or commented on
2. **Authentication**: Uses JIRA API token for posting comments and updating issues
3. **Project Filtering**: Only processes issues from configured project keys
4. **Label-Based Activation**: Only processes issues with the `ai-assist` label (configurable)
5. **Signature Verification**: Webhooks are verified using HMAC-SHA256 signature

### Data Flow with Real JIRA

```
JIRA Issue Created
        │
        ▼
┌─────────────────┐
│  Webhook Sent   │  POST /webhook/jira
│  to Your Server │  Headers: X-Jira-Event, X-Jira-Signature
└────────┬────────┘
         │
         ▼
┌─────────────────┐     ┌─────────────────┐
│  Signature      │────▶│  Extract Issue  │
│  Verified?      │     │  Data (key,     │
└────────┬────────┘     │  summary, etc.) │
         │              └────────┬────────┘
    Yes  │                       │
         ▼                       ▼
┌─────────────────┐     ┌─────────────────┐
│  Workflow       │────▶│  Execute Agent  │
│  Router         │     │  (Sisyphus/     │
│  (Plan/Direct)  │     │  Prometheus)    │
└─────────────────┘     └────────┬────────┘
                                 │
                    ┌────────────┼────────────┐
                    │            │            │
                    ▼            ▼            ▼
            ┌──────────┐  ┌──────────┐  ┌──────────┐
            │ Progress │  │  Output  │  │  Errors  │
            │  Updates │  │  Files   │  │          │
            └────┬─────┘  └────┬─────┘  └────┬─────┘
                 │             │             │
                 └─────────────┴─────────────┘
                               │
                               ▼
                    ┌─────────────────┐
                    │  Post Comments  │  POST /rest/api/2/issue/{key}/comment
                    │  to JIRA Issue  │  Auth: Bearer {API_TOKEN}
                    └─────────────────┘
```

## Architecture

The JIRA Virtual Developer consists of these main components:

### Component Overview

| Component | Purpose | Technology |
|-----------|---------|------------|
| **Webhook Server** | Receives JIRA events | FastAPI (Python) |
| **Job Processor** | Routes issues to workflows | Python asyncio |
| **Agent Runner** | Executes Oh My OpenAgent | Bun subprocess |
| **JIRA Client** | API communication | Requests + JIRA REST API |
| **State Manager** | Tracks issue progress | JSON files |
| **Reporter** | Posts updates to JIRA | JIRA REST API |

### Agent Types

| Agent | Purpose | When Used |
|-------|---------|-----------|
| **Sisyphus** | Direct task execution | Simple issues (bugs, typos, fixes) |
| **Prometheus** | Planning and analysis | Complex issues requiring multi-step plans |
| **Atlas** | Orchestration | Executing plans with multiple sub-tasks |
| **Oracle** | Architecture consultation | When user asks questions with @mention |

### Workflow Types

The system automatically detects the appropriate workflow:

**1. Direct Execution (Sisyphus)**
- Triggered by: Simple issues with keywords like "fix", "typo", "bug", "error"
- Process: Sisyphus analyzes → fixes → commits changes
- Duration: Typically 30 seconds to 5 minutes

**2. Planning → Execution (Prometheus → Atlas)**
- Triggered by: Complex issues with keywords like "implement", "add feature", "refactor"
- Process: 
  - Prometheus creates detailed plan (.sisyphus/plans/)
  - Plan posted to JIRA for review
  - User comments `/start-work`
  - Atlas orchestrates execution
- Duration: 5 minutes to 30+ minutes depending on complexity

**3. Oracle Consultation**
- Triggered by: Comments with @BotName asking questions
- Process: Oracle analyzes codebase → provides architecture advice
- Duration: 1-2 minutes

## Quick Start

### 1. Installation

#### Linux/Mac

```bash
# Clone or copy the project
cd jira_virtual_developer

# Run the automated install script
./install.sh

# Or install manually:
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Install OpenCode CLI
npm install -g opencode

# Install oh-my-opencode plugin
bunx oh-my-opencode install

# Initialize project structure
python cli.py init
```

#### Windows (recommended: offline zip)

CI builds a self-contained Windows zip (`virtual_developer-windows-x64-*.zip`) that
already includes pinned **OpenCode**, **oh-my-opencode**, **glab**, and **Python wheels**.

```cmd
:: 1) Download the artifact from GitHub Actions (or the Release zip)
:: 2) Extract ONCE — you should see install.bat next to vendor\ and src\
::    (no "zip inside zip"; do not re-extract vendor files)
:: 3) Use a supported Python (see vendor\SUPPORTED_PYTHON.txt — usually 3.10–3.13)
:: 4) Install
install.bat
```

**Important:** Python **3.14** is often **not** supported yet (packages like `pydantic-core`
may lack wheels). Prefer **Python 3.12 x64** for the smoothest offline install.

What `install.bat` does:

| Step | Result |
|------|--------|
| Python venv | Creates `.venv` and installs deps from `vendor\python-wheels` (offline; **3.10–3.13**) |
| OpenCode | Extracts `vendor\opencode-home.zip` → **`%USERPROFILE%\.opencode`** |
| glab | Places `glab.exe` in `%USERPROFILE%\.opencode\bin` |
| PATH | Adds `%USERPROFILE%\.opencode\bin` to the **user** PATH |
| Project | Creates `.env` from `.env.example` if missing; runs `cli.py init` |

The outer zip does **not** expand `node_modules` (avoids Windows path-too-long and slow
Explorer extract). OpenCode + plugin ship as one file: `vendor\opencode-home.zip`,
which `install.bat` unpacks into **`%USERPROFILE%\.opencode` only** (no second copy under `C:\vd`).

OpenCode layout after install:

```text
%USERPROFILE%\.opencode\
  bin\opencode.exe
  bin\glab.exe
  opencode.json          # plugin registration (valid JSON)
  oh-my-opencode.json
  package.json
  node_modules\...       # extracted by install.bat from opencode-home.zip

%USERPROFILE%\.config\opencode\
  opencode.json          # mirrored for OpenCode global config discovery
```

**Windows requirements (zip install):**

- **Windows 10/11 64-bit (x64 / AMD64)** — OpenCode is the official `opencode-windows-x64` build (not 32-bit, not ARM)
- **Python 3.10+** (64-bit; see `vendor\SUPPORTED_PYTHON.txt`) from https://www.python.org — enable “Add to PATH”
- Git for Windows (recommended)
- **No Node.js/npm required** when using the CI zip

If Windows says OpenCode is “not compatible with 64-bit Windows”, the binary is almost always
**corrupt/incomplete** (bad extract) or an older `opencode` is earlier on PATH. Delete
`%USERPROFILE%\.opencode`, remove any leftover `C:\vd\opencode`, re-install from a fresh
package, open a **new** terminal, then run `where opencode` (expect a single path under
`%USERPROFILE%\.opencode\bin`).

If `opencode` reports **invalid JSON** for `.config\opencode\opencode.json`, delete that
file and re-run `install.bat` (older installer builds could overwrite the config via a
cmd `echo` redirect bug).

**From a git clone (online fallback):** the same `install.bat` works without `vendor\`;
set `VD_ALLOW_ONLINE=1` and it will download OpenCode/glab and use npm for the plugin if available.

Pinned versions live in `packaging/windows/versions.env` and are refreshed by
`.github/workflows/windows-dist.yml` on pushes to `main`/`develop` and on `v*` tags.

### 2. Configuration

```bash
# Copy example configuration
cp .env.example .env

# Edit with your settings (see Configuration section below)
nano .env
```

### 3. Test Without JIRA (Recommended First Step)

The project includes a **sample calculator with intentional bugs** for testing:

```bash
# View the sample project
cd sample_project
cat src/calculator/calc.py  # See the bugs

# Run tests to see failures
pip install -e ".[test]"
pytest  # Some tests will fail

# Go back to agent directory
cd ..

# Test the agent - fix calculator bugs
python cli.py test-issue \
  --project sample_project \
  --title "Fix calculator bugs" \
  --description "Fix all bugs in src/calculator/calc.py. The divide method doesn't handle division by zero, power uses wrong operator, average doesn't check for empty list, and factorial has wrong base case."

# Test with specific agent/category
python cli.py test-issue \
  --title "Refactor calculator" \
  --description "Refactor the Calculator class to use type hints properly" \
  --agent sisyphus \
  --category deep

# Create a plan only (no execution)
python cli.py test-issue \
  --title "Add new features" \
  --description "Add modulo, square root, and trigonometric functions" \
  --plan-only

# Dry run to see what would happen
python cli.py test-issue \
  --title "Test dry run" \
  --description "This won't actually run" \
  --dry-run
```

**Sample Project Bugs:**
1. `divide()` - No division by zero check
2. `power()` - Uses `*` instead of `**`
3. `average()` - No empty list check
4. `factorial()` - Wrong base case (returns 0 instead of 1)

### 4. Simulated JIRA Server (PoC Mode)

For a more realistic testing experience, use the **Simulated JIRA Server**. This mimics a real JIRA environment with webhooks, issues, and comments.

**Architecture:**
```
┌─────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  Simulated      │────▶│  JIRA Virtual    │────▶│   Oh My          │
│  JIRA Server    │     │  Developer       │     │   OpenAgent      │
│  (Port 7001)    │     │  (Port 7000)     │     │                  │
└─────────────────┘     └──────────────────┘     └──────────────────┘
```

**Quick Start:**

```bash
# Terminal 1: Start the simulated JIRA server
python cli.py simulate start-server

# Terminal 2: Create an issue and notify the bot
python cli.py simulate create-issue \
  --summary "Fix calculator bugs" \
  --description "Fix all bugs in calculator/calc.py" \
  --assignee "DevBot" \
  --labels "ai-assist,bug"

# The bot will automatically receive the webhook and start working!
```

**Available Commands:**

```bash
# Start the simulated JIRA server (runs on port 7001)
python cli.py simulate start-server
python cli.py simulate start-server --port 7001 --webhook-port 7000

# Create a new issue and notify the bot
python cli.py simulate create-issue \
  --summary "Task title" \
  --description "Create a main.py file add "2+3" then open a pr" \
  --assignee "DevBot" \
  --labels "ai-assist"


python cli.py simulate create-issue --summary "Task title"  --description "Create a main.py file add '2+3' "  --assignee "DevBot"  --labels "ai-assist"

set NODE_EXTRA_CA_CERTS=

# Manually notify the bot about an issue
python cli.py simulate notify \
  --summary "Fix bugs" \
  --description "Fix the calculator"

# List all issues in the simulated JIRA
python cli.py simulate list-issues

# Show details for a specific issue
python cli.py simulate show-issue SIM-1001
```

**API Endpoints (for custom integrations):**

The simulated JIRA server provides these REST API endpoints:

- `GET  /api/issues` - List all issues
- `POST /api/issues` - Create new issue
- `GET  /api/issues/<key>` - Get issue details
- `PUT  /api/issues/<key>` - Update issue
- `POST /api/issues/<key>/comments` - Add comment
- `POST /api/issues/<key>/assign` - Assign issue
- `POST /api/webhook` - Register webhook URL
- `POST /api/notify` - Manual bot notification

**Example using curl:**

```bash
# Create an issue
curl -X POST http://localhost:7001/api/issues \
  -H "Content-Type: application/json" \
  -d '{
    "summary": "Fix bugs",
    "description": "Fix calculator bugs",
    "assignee": "DevBot",
    "labels": ["ai-assist"]
  }'

# Notify the bot
curl -X POST http://localhost:7001/api/notify \
  -H "Content-Type: application/json" \
  -d '{
    "summary": "Fix bugs",
    "description": "Fix calculator bugs",
    "assignee": "DevBot",
    "labels": ["ai-assist"]
  }'
```

### 5. Run the Daemon (With JIRA)

```bash
# Start the daemon
python cli.py start

# Or with webhook only
ENABLE_WEBHOOK=true ENABLE_POLLING=false python cli.py start

# Or with polling only
ENABLE_WEBHOOK=false ENABLE_POLLING=true python cli.py start
```

## Usage

### Automatic Workflows

**Complex Issues** (Planning → Execution):
1. Create JIRA issue with label `ai-assist`
2. Bot acknowledges and starts Prometheus planning
3. Plan is posted to JIRA as comment
4. User reviews and comments `/start-work`
5. Atlas orchestrates execution
6. Results posted to JIRA

**Simple Issues** (Direct execution):
1. Create issue labeled `ai-assist` with keywords like "fix", "typo"
2. Bot routes to direct execution
3. Sisyphus completes work
4. Results posted

### Bot Commands

Mention the bot in a JIRA comment:

```
@DevBot /start-work     # Start plan execution
@DevBot /status         # Check current status
@DevBot /cancel         # Cancel current work
@DevBot fix the typo in line 42  # Direct request
```

### CLI Commands

**Setup & Configuration:**
```bash
# Initialize project structure
python cli.py init

# Show configuration
python cli.py config

# Run installation script
./install.sh
```

**Testing Without JIRA:**
```bash
# Test agent with a task (no JIRA required)
python cli.py test-issue --title "Task title" --description "Task description"

# Options:
#   --project PATH       Project directory (default: sample_project)
#   --agent NAME         Agent: sisyphus, prometheus, atlas, oracle
#   --category NAME      Category: quick, deep, visual-engineering, etc.
#   --plan-only          Only create plan (Prometheus)
#   --dry-run            Show what would happen without running
```

**Simulated JIRA Server (PoC Mode):**
```bash
# Start the simulated JIRA server
python cli.py simulate start-server
python cli.py simulate start-server --port 7001 --webhook-port 7000

# Create an issue and notify the bot
python cli.py simulate create-issue \
  --summary "Task title" \
  --description "Task description" \
  --assignee "DevBot" \
  --labels "ai-assist"

# Manually notify the bot
python cli.py simulate notify \
  --summary "Task title" \
  --description "Task description"

# List all issues
python cli.py simulate list-issues

# Show issue details
python cli.py simulate show-issue SIM-1001
```

**JIRA Commands (requires JIRA config):**
```bash
# Show active issues
python cli.py status

# Show issue details
python cli.py show PROJ-123

# Manually process an issue
python cli.py process PROJ-123

# Cancel an issue
python cli.py cancel PROJ-123
```

## Configuration Options

### JIRA Connection

| Variable | Description | Default |
|----------|-------------|---------|
| `JIRA_HOST` | JIRA instance URL | Required |
| `JIRA_API_TOKEN` | API token (Bearer) for authentication | Required |
| `JIRA_PROJECTS` | Comma-separated project keys | `PROJ` |

### Webhook & Server

| Variable | Description | Default |
|----------|-------------|---------|
| `WEBHOOK_PORT` | Port for webhook server | `3000` |
| `WEBHOOK_PATH` | Webhook endpoint path | `/webhook/jira` |
| `WEBHOOK_SECRET` | Secret for webhook signature verification | None |
| `ENABLE_WEBHOOK` | Enable webhook server | `true` |
| `ENABLE_POLLING` | Enable JIRA polling fallback | `false` |
| `POLL_INTERVAL_SECONDS` | Polling interval in seconds | `30` |

### Agent Selection

| Variable | Description | Default |
|----------|-------------|---------|
| `DEFAULT_AGENT` | Default agent for direct tasks | `sisyphus` |
| `PLANNING_AGENT` | Agent for planning | `prometheus` |
| `ORCHESTRATOR_AGENT` | Agent for execution | `atlas` |
| `EXECUTION_CATEGORY` | Category for task execution | `deep` |

### Agent Task Configuration (Timeout & Retry)

| Variable | Description | Default |
|----------|-------------|---------|
| `AGENT_TASK_TIMEOUT_SECONDS` | Maximum time for agent task to complete | `1800` (30 min) |
| `AGENT_TASK_MAX_RETRIES` | Maximum retry attempts for failed tasks | `3` |
| `AGENT_TASK_RETRY_DELAY_SECONDS` | Initial delay between retries | `5` |
| `AGENT_TASK_RETRY_BACKOFF_MULTIPLIER` | Exponential backoff multiplier | `2.0` |
| `AGENT_TASK_RETRY_ON_TIMEOUT` | Retry tasks that timeout | `true` |
| `AGENT_TASK_RETRY_ON_ERROR` | Retry tasks that fail with errors | `true` |

The retry mechanism uses exponential backoff. For example, with default settings:
- Retry 1: 5 seconds delay
- Retry 2: 10 seconds delay (5 × 2)
- Retry 3: 20 seconds delay (5 × 2²)

### Behavior & Features

| Variable | Description | Default |
|----------|-------------|---------|
| `AUTO_START_PLANS` | Auto-start after planning | `false` |
| `MAX_CONCURRENT_JOBS` | Max parallel jobs | `3` |
| `TRIGGER_LABELS` | Labels that trigger bot (comma-separated) | `ai-assist,bot` |
| `TRIGGER_ON_ASSIGNMENT` | Trigger when issue is assigned | `true` |
| `TRIGGER_MENTIONS` | @mentions that trigger bot (comma-separated) | `@DevBot,@AI` |

### Git Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `PROJECT_ROOT` | Target project directory | Current directory |
| `PROJECT_GITLAB_URL` | GitLab repo URL for cloning | None |
| `GITLAB_PAT` | GitLab Personal Access Token | None |
| `GIT_USER_NAME` | Git user name for commits | `DevBot` |
| `GIT_USER_EMAIL` | Git user email for commits | `devbot@example.com` |

## Real JIRA Server Setup

This section explains how to connect the JIRA Virtual Developer to your actual JIRA instance (Cloud or Server/Data Center).

### Prerequisites

1. **JIRA Instance**: Access to JIRA Cloud or JIRA Server/Data Center
2. **API Token**: For JIRA Cloud, generate at [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-api-tokens)
3. **Network Access**: Your server must be accessible from JIRA (for webhooks)
4. **Project Permissions**: Your JIRA user needs:
   - Browse Projects
   - Create Issues
   - Edit Issues
   - Add Comments
   - Transition Issues (optional)

### Step 1: Get JIRA API Token

**For JIRA Cloud:**
1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Click "Create API token"
3. Give it a name like "JIRA Virtual Developer"
4. Copy the token (you won't see it again!)

**For JIRA Server/Data Center:**
1. Go to your profile → Account Settings → Security
2. Generate a Personal Access Token
3. Or use username + password (not recommended for production)

### Step 2: Configure Environment

Edit your `.env` file:

```env
# JIRA Connection (Required for real JIRA)
JIRA_HOST=https://your-jira.example.com
JIRA_API_TOKEN=your-api-token-from-step-1
JIRA_PROJECTS=PROJ,DEV,ENG  # Comma-separated project keys

# Webhook Configuration
WEBHOOK_PORT=7000
WEBHOOK_SECRET=your-secret-key-here  # Used to verify webhook signatures
WEBHOOK_PATH=/webhook/jira

# Oh My OpenAgent Configuration
OPENCODE_CLI=bunx oh-my-opencode
PROJECT_ROOT=/path/to/your/codebase

# Agent Selection
DEFAULT_AGENT=sisyphus
PLANNING_AGENT=prometheus
ORCHESTRATOR_AGENT=atlas

# Behavior
AUTO_START_PLANS=false
EXECUTION_CATEGORY=deep
MAX_CONCURRENT_JOBS=3

# Features
ENABLE_WEBHOOK=true
ENABLE_POLLING=false
```

### Step 3: Start the Daemon

```bash
# Start the JIRA Virtual Developer
python cli.py start

# Or with specific settings
WEBHOOK_PORT=7000 python cli.py start

# Run in background (Linux/Mac)
nohup python cli.py start > jira-agent.log 2>&1 &
```

### Step 4: Verify Connection

```bash
# Check configuration
python cli.py config

# Test with a specific issue
python cli.py process PROJ-123

# View status
python cli.py status
```

## Webhook Configuration

Webhooks are how JIRA notifies your bot when issues are created or updated. This is **critical** for real-time operation.

### JIRA Cloud Webhook Setup

1. **Navigate to Webhooks:**
   - JIRA Settings (gear icon) → System → WebHooks
   - Or directly: `https://yourcompany.atlassian.net/plugins/servlet/webhooks`

2. **Create New Webhook:**
   - Click "Create a WebHook"
   - Name: "AI Virtual Developer"
   - Status: Enabled
   - URL: `http://your-server:7000/webhook/jira`
   - Description: "Webhook for AI agent integration"

3. **Configure Events:**
   - Issue → created
   - Issue → updated
   - Comment → created
   - Comment → updated (optional)

4. **Set Secret:**
   - In "Advanced" section, set "Secret" to match your `WEBHOOK_SECRET` env var
   - This ensures only JIRA can send valid webhooks

5. **Issue Filters (Recommended):**
   - Add JQL: `labels = "ai-assist"`
   - This ensures only issues with the `ai-assist` label trigger the bot

### JIRA Server/Data Center Webhook Setup

1. **Navigate to:**
   - Administration → System → Advanced → WebHooks

2. **Same configuration as Cloud**, but note:
   - Your server must be accessible from the JIRA server network
   - If behind firewall, whitelist JIRA server IPs
   - For HTTPS, ensure valid SSL certificate

### Webhook Payload Structure

When an issue is created, JIRA sends:

```json
{
  "timestamp": 1234567890,
  "webhookEvent": "jira:issue_created",
  "issue_event_type_name": "issue_created",
  "user": {
    "self": "https://...",
    "accountId": "...",
    "displayName": "John Doe"
  },
  "issue": {
    "id": "10001",
    "self": "https://...",
    "key": "PROJ-123",
    "fields": {
      "summary": "Fix calculator bugs",
      "description": "The divide method doesn't handle division by zero...",
      "issuetype": { "name": "Bug" },
      "project": { "key": "PROJ" },
      "labels": ["ai-assist", "bug"],
      "priority": { "name": "High" },
      "status": { "name": "To Do" }
    }
  }
}
```

### Webhook Security

The bot verifies webhook signatures using HMAC-SHA256:

```python
# JIRA sends header: X-Hub-Signature: sha256=<signature>
# Signature = HMAC-SHA256(WEBHOOK_SECRET, request_body)
```

**If signature verification fails**, the webhook is rejected with 401 Unauthorized.

### Troubleshooting Webhooks

**Check if webhooks are being sent:**
```bash
# On your server, watch incoming requests
tail -f jira-agent.log

# Or use netcat to test
nc -l 7000
```

**Verify webhook URL is accessible:**
```bash
# From another machine
curl -X POST http://your-server:7000/webhook/jira \
  -H "Content-Type: application/json" \
  -d '{"test": true}'

# Should return 401 (unauthorized without signature)
```

**Common webhook issues:**

| Issue | Cause | Solution |
|-------|-------|----------|
| 401 Unauthorized | Signature mismatch | Check WEBHOOK_SECRET matches JIRA |
| 404 Not Found | Wrong path | Ensure URL ends with `/webhook/jira` |
| Connection refused | Firewall/port | Open port 7000, check firewall |
| No events received | JQL filter | Remove JQL filter for testing |
| Events but no action | Project filter | Check JIRA_PROJECTS includes your project |

## State Management

State is stored in `.jira-agent/state/` as JSON files:

```
.jira-agent/
├── state/
│   ├── PROJ_123.json         # Issue state
│   ├── PROJ_124.json
│   └── TEST_20240327.json
├── sessions/
│   ├── session_abc123.log    # Agent output logs
│   └── session_def456.log
└── plans/
    └── plan_ghi789.md        # Generated plans
```

### State File Structure

Each issue state file contains:

```json
{
  "issue_key": "PROJ-123",
  "issue_summary": "Fix calculator bugs",
  "status": "completed",
  "progress_percentage": 100,
  "workflow_type": "direct_execution",
  "current_agent": "sisyphus",
  "started_at": "2024-03-27T10:00:00",
  "completed_at": "2024-03-27T10:05:30",
  "execution_duration_seconds": 330.5,
  "token_usage_input": 1500,
  "token_usage_output": 2500,
  "estimated_cost": 0.045,
  "plan_path": ".sisyphus/plans/PROJ-123_plan.md",
  "error_message": null,
  "retry_count": 2,
  "max_retries": 3,
  "last_retry_at": "2024-03-27T10:02:15",
  "retry_reason": "timeout",
  "timed_out": false,
  "timeout_seconds": 1800
}
```

### Timeout and Retry Tracking

The system tracks timeout and retry information for each issue:

- **`timed_out`**: Set to `true` if the task exceeded the configured timeout
- **`retry_count`**: Number of retry attempts made
- **`max_retries`**: Maximum retry attempts configured
- **`last_retry_at`**: Timestamp of the last retry attempt
- **`retry_reason`**: Reason for the last retry (`timeout` or `error`)
- **`timeout_seconds`**: Timeout configuration used for this task

### Cost Tracking

The system tracks API usage costs:

```bash
# View cost summary across all issues
python cli.py costs
```

Example output:
```
💰 Cost Summary
┌──────────────────┬──────────┐
│ Metric           │ Value    │
├──────────────────┼──────────┤
│ Total Issues     │ 12       │
│ Total Duration   │ 1856.3s  │
│ Input Tokens     │ 45,230   │
│ Output Tokens    │ 78,450   │
│ Total Tokens     │ 123,680  │
│ Estimated Cost   │ $2.3456  │
│ Avg Cost/Issue   │ $0.1955  │
└──────────────────┴──────────┘
```

Cost calculation uses approximate token pricing:
- Input: $0.00001 per token
- Output: $0.00003 per token

## Troubleshooting

### Connection Issues

**Cannot connect to JIRA:**
```bash
# Test JIRA API access
curl -u your-email@example.com:your-api-token \
  https://yourcompany.atlassian.net/rest/api/2/myself

# Should return your user info
```

**Webhook not receiving events:**
- Check firewall: `sudo ufw allow 7000/tcp`
- Verify URL is accessible from internet (use ngrok for local testing)
- Check JIRA webhook logs (System → Troubleshooting → Logs)
- Ensure `WEBHOOK_SECRET` matches exactly

### Agent Issues

**Agent not starting:**
```bash
# Verify OpenCode CLI works
bunx oh-my-opencode --version

# Check project root exists
ls $PROJECT_ROOT

# Ensure .sisyphus directory exists
mkdir -p $PROJECT_ROOT/.sisyphus/plans
```

**Agent fails immediately:**
- Check session logs: `cat .jira-agent/sessions/session_*.log`
- Verify oh-my-opencode plugin is installed: `bunx oh-my-opencode install`
- Check for syntax errors in your codebase

**Task timeouts:**
- Check if task is genuinely taking too long: `python cli.py show PROJ-123`
- Increase timeout: `AGENT_TASK_TIMEOUT_SECONDS=3600 python cli.py start`
- Check for infinite loops or hanging processes in agent output

**Excessive retries:**
- Check retry configuration: `python cli.py config`
- View retry history: `python cli.py show PROJ-123`
- Disable retries for testing: `AGENT_TASK_MAX_RETRIES=0 python cli.py start`

### JIRA API Errors

**401 Unauthorized:**
- API token expired or revoked
- User doesn't have project permissions
- Wrong JIRA_HOST format (should be full URL)

**403 Forbidden:**
- User lacks permissions on project
- API token doesn't have required scopes

**404 Not Found:**
- Issue key doesn't exist
- Project key is wrong

### Debug Mode

Enable verbose logging:

```bash
# Set debug environment variable
DEBUG=true python cli.py start

# Or for specific commands
DEBUG=true python cli.py process PROJ-123
```

### Getting Help

1. Check logs: `tail -f jira-agent.log` (Linux/Mac) or `type jira-agent.log` (Windows)
2. Verify config: `python cli.py config`
3. Test without JIRA: `python cli.py test-issue --title "Test" --description "Test"`
4. Check agent output: `python cli.py show PROJ-123`

### Windows-Specific Issues

**'python' is not recognized:**
- Make sure Python is added to your PATH during installation
- Or use `py` instead of `python`

**'npm' is not recognized:**
- Restart your terminal after installing Node.js
- Ensure Node.js is added to your PATH

**Agent fails with "file not found" errors:**
- Check that paths in `.env` use Windows format: `PROJECT_ROOT=C:\Users\name\project`
- Or use forward slashes: `PROJECT_ROOT=C:/Users/name/project`

**Port already in use:**
```cmd
:: Find process using port 7000
netstat -ano | findstr :7000
:: Kill the process (replace PID with actual number)
taskkill /PID <PID> /F
```

**Unicode/character encoding issues:**
- Ensure your terminal supports UTF-8 (Windows Terminal recommended)
- Or set environment variable: `chcp 65001`

## Security Considerations

1. **API Tokens**: Store in `.env`, never commit to git
2. **Webhook Secret**: Use strong random string, rotate periodically
3. **Network**: Use HTTPS in production (reverse proxy recommended)
4. **Permissions**: Use dedicated JIRA user with minimal permissions
5. **Rate Limiting**: JIRA has API rate limits (check your plan)

## License

MIT
