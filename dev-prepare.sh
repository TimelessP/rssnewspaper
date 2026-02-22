#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== RSS Newspaper — Dev Prepare ==="

# Create .venv if it doesn't exist
if [[ ! -d .venv ]]; then
    echo "Creating virtual environment with python3.12..."
    python3.12 -m venv .venv
else
    echo "Virtual environment already exists."
fi

# Activate
source .venv/bin/activate
echo "Activated: $(python --version) at $(which python)"

# Copy .env from example if missing
if [[ ! -f .env ]]; then
    echo "Copying example.env → .env"
    cp example.env .env
else
    echo ".env already exists."
fi

# Install Python dependencies
echo "Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

# Check for Claude Code CLI
if command -v claude &>/dev/null; then
    echo "Claude Code CLI: $(claude --version 2>/dev/null || echo 'installed')"
else
    echo ""
    echo "⚠  Claude Code CLI not found."
    echo "   Install it with:  npm install -g @anthropic-ai/claude-code"
    echo ""
fi

echo ""
echo "Done. To activate the venv in your shell:"
echo "  source .venv/bin/activate"
