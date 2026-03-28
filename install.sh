#!/bin/bash
# JIRA Virtual Developer - Installation Script
# This script sets up the environment for testing without JIRA

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  JIRA Virtual Developer - Installer${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Check if Python 3 is installed
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: Python 3 is not installed${NC}"
    echo "Please install Python 3.8 or higher"
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo -e "${GREEN}✓ Python 3 found: $PYTHON_VERSION${NC}"

# Check if pip is installed
if ! command -v pip3 &> /dev/null; then
    echo -e "${RED}Error: pip3 is not installed${NC}"
    exit 1
fi
echo -e "${GREEN}✓ pip3 found${NC}"

# Check if npm is installed
if ! command -v npm &> /dev/null; then
    echo -e "${RED}Error: npm is not installed${NC}"
    echo "Please install Node.js and npm"
    exit 1
fi
echo -e "${GREEN}✓ npm found${NC}"

echo ""
echo -e "${BLUE}Step 1: Installing Python dependencies...${NC}"
cd "$SCRIPT_DIR"
pip3 install -r requirements.txt --quiet --break-system-packages
echo -e "${GREEN}✓ Python dependencies installed${NC}"


echo ""
echo -e "${BLUE}Step 3: Installing OpenCode CLI...${NC}"
if ! command -v opencode &> /dev/null; then
    curl -fsSL https://opencode.ai/install | bash
    echo -e "${GREEN}✓ OpenCode CLI installed${NC}"
else
    echo -e "${GREEN}✓ OpenCode CLI already installed${NC}"
fi

echo ""
echo -e "${BLUE}Step 3b: Installing GitLab CLI (glab)...${NC}"
if ! command -v glab &> /dev/null; then
    # Try brew (macOS)
    if command -v brew &> /dev/null; then
        brew install glab
    # Try apt (Debian/Ubuntu)
    elif command -v apt-get &> /dev/null; then
        curl -fsSL https://packages.gitlab.com/install/repositories/gitlab/gitlab-ce/script.deb.sh | bash 2>/dev/null || true
        apt-get install -y gitlab-cli 2>/dev/null || echo "  (glab install via apt may require manual setup)"
    else
        echo -e "${YELLOW}  Please install 'glab' manually: https://gitlab.com/gitlab-org/cli${NC}"
    fi
    if command -v glab &> /dev/null; then
        echo -e "${GREEN}✓ GitLab CLI (glab) installed${NC}"
    fi
else
    echo -e "${GREEN}✓ GitLab CLI (glab) already installed${NC}"
fi

echo ""
echo -e "${BLUE}Step 4: Installing oh-my-opencode...${NC}"
# Install oh-my-opencode globally
npm install -g oh-my-opencode --silent
echo -e "${GREEN}✓ oh-my-opencode installed globally${NC}"

# Install oh-my-opencode as opencode plugin
OPENCODE_DIR="$HOME/.opencode"
mkdir -p "$OPENCODE_DIR"
cd "$OPENCODE_DIR"

# Create package.json if it doesn't exist
if [ ! -f "package.json" ]; then
    echo '{"dependencies": {}}' > package.json
fi

# Install plugin
npm install oh-my-opencode --silent
echo -e "${GREEN}✓ oh-my-opencode plugin installed${NC}"

echo ""
echo -e "${BLUE}Step 5: Configuring oh-my-opencode...${NC}"
export PATH="$HOME/.opencode/bin:$PATH"

# Run non-interactive install
oh-my-opencode install --no-tui \
    --claude=no \
    --openai=no \
    --gemini=no \
    --copilot=no \
    --opencode-zen=no \
    --zai-coding-plan=no \
    --kimi-for-coding=no \
    --opencode-go=no 2>&1 | grep -v "^$" || true

echo -e "${GREEN}✓ oh-my-opencode configured${NC}"

echo ""
echo -e "${BLUE}Step 6: Initializing JIRA Virtual Developer...${NC}"
cd "$SCRIPT_DIR"
export PATH="$HOME/.opencode/bin:$PATH"
python3 cli.py init
echo -e "${GREEN}✓ JIRA Virtual Developer initialized${NC}"

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}  Installation Complete!${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "${YELLOW}Next Steps:${NC}"
echo ""
echo -e "1. ${GREEN}Test the sample project:${NC}"
echo "   export PATH=\"\$HOME/.opencode/bin:\$PATH\""
echo "   cd $SCRIPT_DIR"
echo "   python3 cli.py test-issue \\"
echo "       --title \"Fix calculator bugs\" \\"
echo "       --description \"Fix all bugs in calculator/calc.py\""
echo ""
echo -e "2. ${GREEN}Run tests on sample project:${NC}"
echo "   cd $SCRIPT_DIR/sample_project"
echo "   .venv/bin/pytest -v"
echo ""
echo -e "3. ${GREEN}To use with JIRA (optional):${NC}"
echo "   Edit .env file with your JIRA credentials:"
echo "   - JIRA_HOST=https://yourcompany.atlassian.net"
echo "   - JIRA_USERNAME=your-email@example.com"
echo "   - JIRA_API_TOKEN=your-api-token"
echo ""
echo -e "${YELLOW}Note:${NC} Make sure to add the following to your shell profile:"
echo -e "   ${BLUE}export PATH=\"\$HOME/.opencode/bin:\$PATH\"${NC}"
echo ""
