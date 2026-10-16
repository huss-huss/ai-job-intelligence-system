#!/bin/bash
# Script to add GitHub secrets using GitHub CLI (interactive)
# This will prompt you to authenticate if needed

set -e

eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true

cd ~/Desktop/ai-job-intelligence-system

echo "🔐 Setting up GitHub secrets..."
echo ""

# Check if authenticated
if ! gh auth status &>/dev/null; then
    echo "⚠️  Not authenticated with GitHub CLI"
    echo "📝 Please authenticate:"
    echo ""
    gh auth login
    echo ""
fi

echo "✅ Authenticated!"
echo ""

# Add secrets
echo "Adding VPS_SSH_KEY..."
gh secret set VPS_SSH_KEY < /Users/hussen/.ssh/vps_deploy_key

echo "Adding VPS_HOST..."
gh secret set VPS_HOST --body "${VPS_HOST}"

echo "Adding VPS_USER..."
gh secret set VPS_USER --body "hussen"

echo ""
echo "✅ All secrets added!"
echo ""
echo "Verify at: https://github.com/huss-huss/ai-job-intelligence-system/settings/secrets/actions"
echo ""
gh secret list
