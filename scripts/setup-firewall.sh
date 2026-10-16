#!/bin/bash
# Firewall setup script for AI Job Intelligence System
# Run with: sudo ./scripts/setup-firewall.sh

set -e

echo "🔥 Setting up UFW firewall..."

# Check if UFW is installed
if ! command -v ufw &> /dev/null; then
    echo "Installing UFW..."
    sudo apt-get update
    sudo apt-get install -y ufw
fi

# Enable UFW if not already enabled
echo "Enabling UFW..."
sudo ufw --force enable || true

# Allow SSH (important - don't lock yourself out!)
echo "Adding SSH rule..."
sudo ufw allow 22/tcp comment 'SSH'

# Allow HTTP
echo "Adding HTTP rule..."
sudo ufw allow 80/tcp comment 'HTTP'

# Allow HTTPS
echo "Adding HTTPS rule..."
sudo ufw allow 443/tcp comment 'HTTPS'

# Allow Backend API (if needed externally)
echo "Adding Backend API rule..."
sudo ufw allow 8000/tcp comment 'Backend API'

# Show status
echo ""
echo "✅ Firewall rules configured:"
sudo ufw status numbered

echo ""
echo "✅ Firewall setup complete!"
