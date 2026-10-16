#!/bin/bash
# Script to add GitHub secrets using GitHub API
# Usage: ./scripts/add-github-secrets.sh <GITHUB_TOKEN>

set -e

GITHUB_TOKEN="${1:-${GH_TOKEN}}"
REPO="huss-huss/ai-job-intelligence-system"

if [ -z "$GITHUB_TOKEN" ]; then
    echo "❌ Error: GitHub token required"
    echo ""
    echo "Usage:"
    echo "  ./scripts/add-github-secrets.sh <your_github_token>"
    echo ""
    echo "Or set GH_TOKEN environment variable:"
    echo "  export GH_TOKEN=your_token"
    echo "  ./scripts/add-github-secrets.sh"
    echo ""
    echo "To get a token:"
    echo "  1. Go to: https://github.com/settings/tokens"
    echo "  2. Generate new token (classic)"
    echo "  3. Select scope: 'repo' (full control)"
    echo "  4. Copy the token"
    exit 1
fi

echo "🔐 Adding GitHub secrets..."

# Read private key
PRIVATE_KEY=$(cat /Users/hussen/.ssh/vps_deploy_key)

# Add VPS_SSH_KEY
echo "Adding VPS_SSH_KEY..."
curl -X PUT \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/repos/$REPO/actions/secrets/VPS_SSH_KEY" \
  -d "{\"encrypted_value\":\"$(echo -n "$PRIVATE_KEY" | base64)\",\"key_id\":\"$(curl -s -H \"Authorization: token $GITHUB_TOKEN\" \"https://api.github.com/repos/$REPO/actions/secrets/public-key\" | grep -o '\"key_id\":\"[^\"]*\"' | cut -d'\"' -f4)\"}" \
  2>/dev/null | grep -q "created\|updated" && echo "✅ VPS_SSH_KEY added" || echo "⚠️  VPS_SSH_KEY may have failed"

# Add VPS_HOST
echo "Adding VPS_HOST..."
curl -X PUT \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/repos/$REPO/actions/secrets/VPS_HOST" \
  -d "{\"encrypted_value\":\"$(echo -n "$VPS_HOST" | base64)\",\"key_id\":\"$(curl -s -H \"Authorization: token $GITHUB_TOKEN\" \"https://api.github.com/repos/$REPO/actions/secrets/public-key\" | grep -o '\"key_id\":\"[^\"]*\"' | cut -d'\"' -f4)\"}" \
  2>/dev/null | grep -q "created\|updated" && echo "✅ VPS_HOST added" || echo "⚠️  VPS_HOST may have failed"

# Add VPS_USER
echo "Adding VPS_USER..."
curl -X PUT \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/repos/$REPO/actions/secrets/VPS_USER" \
  -d "{\"encrypted_value\":\"$(echo -n 'hussen' | base64)\",\"key_id\":\"$(curl -s -H \"Authorization: token $GITHUB_TOKEN\" \"https://api.github.com/repos/$REPO/actions/secrets/public-key\" | grep -o '\"key_id\":\"[^\"]*\"' | cut -d'\"' -f4)\"}" \
  2>/dev/null | grep -q "created\|updated" && echo "✅ VPS_USER added" || echo "⚠️  VPS_USER may have failed"

echo ""
echo "✅ Done! Check secrets at:"
echo "   https://github.com/$REPO/settings/secrets/actions"
