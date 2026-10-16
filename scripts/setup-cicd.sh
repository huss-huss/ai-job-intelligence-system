#!/bin/bash
# CI/CD Setup Helper Script
# This script helps generate SSH keys for GitHub Actions

set -e

echo "🔐 Setting up CI/CD SSH keys for GitHub Actions"
echo ""

# Check if key already exists
KEY_PATH="$HOME/.ssh/vps_deploy_key"
if [ -f "$KEY_PATH" ]; then
    echo "⚠️  SSH key already exists at $KEY_PATH"
    echo "Do you want to overwrite it? (y/N)"
    read -r response
    if [[ ! "$response" =~ ^[Yy]$ ]]; then
        echo "Using existing key..."
    else
        rm -f "$KEY_PATH" "$KEY_PATH.pub"
    fi
fi

# Generate SSH key if it doesn't exist
if [ ! -f "$KEY_PATH" ]; then
    echo "📝 Generating new SSH key..."
    ssh-keygen -t ed25519 -C "github-actions" -f "$KEY_PATH" -N ""
    echo "✅ SSH key generated!"
fi

echo ""
echo "📋 Next steps:"
echo ""
echo "1. Copy your PRIVATE key (keep this secret!):"
echo "   cat $KEY_PATH"
echo ""
echo "2. Add the PUBLIC key to your VPS:"
echo "   ssh-copy-id -i $KEY_PATH.pub ${VPS_USER}@${VPS_HOST}"
echo ""
echo "3. Go to GitHub and add secrets:"
echo "   https://github.com/huss-huss/ai-job-intelligence-system/settings/secrets/actions"
echo ""
echo "   Add these secrets:"
echo "   - VPS_SSH_KEY: (paste private key content)"
echo "   - VPS_HOST: ${VPS_HOST}"
echo "   - VPS_USER: hussen"
echo ""
echo "4. Test SSH connection:"
echo "   ssh -i $KEY_PATH ${VPS_USER}@${VPS_HOST} 'echo Connection successful!'"
echo ""
