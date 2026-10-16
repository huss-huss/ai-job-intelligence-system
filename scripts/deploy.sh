#!/bin/bash
# Deployment script for AI Job Intelligence System
# Run from repo root: ./scripts/deploy.sh

set -e

VPS_USER="${VPS_USER:-hussen}"
VPS_HOST="${VPS_HOST:?Set VPS_HOST to your server address}"
VPS_PATH="${VPS_PATH:-~/ai-job-intelligence-system}"

echo "🚀 Starting deployment to ${VPS_USER}@${VPS_HOST}..."

# Check if we're in the repo root
if [ ! -f "README.md" ] || [ ! -d "backend" ] || [ ! -d "frontend" ]; then
  echo "❌ Error: Must run from repository root"
  exit 1
fi

# Create deployment archive (excluding unnecessary files)
echo "📦 Creating deployment package..."
tar --exclude='.git' \
    --exclude='node_modules' \
    --exclude='.next' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.env' \
    --exclude='.DS_Store' \
    -czf /tmp/ai-job-intelligence-deploy.tar.gz .

# Copy to server
echo "📤 Uploading files to server..."
scp /tmp/ai-job-intelligence-deploy.tar.gz ${VPS_USER}@${VPS_HOST}:/tmp/

# Deploy on server
echo "🔧 Deploying on server..."
ssh ${VPS_USER}@${VPS_HOST} << ENDSSH
set -e
cd ~

# Extract files
if [ -d "${VPS_PATH}" ]; then
  echo "📂 Updating existing deployment..."
  cd ${VPS_PATH}
  tar -xzf /tmp/ai-job-intelligence-deploy.tar.gz
else
  echo "📂 Creating new deployment directory..."
  mkdir -p ${VPS_PATH}
  cd ${VPS_PATH}
  tar -xzf /tmp/ai-job-intelligence-deploy.tar.gz
fi

# Check for .env file
if [ ! -f .env ]; then
  echo "⚠️  Warning: .env file not found. Creating from example..."
  if [ -f infra/.env.example ]; then
    cp infra/.env.example .env
    echo "📝 Please edit .env file with your configuration:"
    echo "   ssh ${VPS_USER}@${VPS_HOST}"
    echo "   nano ${VPS_PATH}/.env"
  else
    echo "❌ Error: infra/.env.example not found"
    exit 1
  fi
fi

# Build frontend (if needed)
if [ -d frontend ]; then
  echo "🎨 Building frontend..."
  cd frontend
  if command -v npm &> /dev/null; then
    npm install --production
    npm run build
  else
    echo "⚠️  npm not found, skipping frontend build"
  fi
  cd ..
fi

# Stop existing containers
echo "🛑 Stopping existing containers..."
docker compose -f infra/docker-compose.prod.yml down || true

# Build and start containers
echo "🐳 Building Docker images..."
docker compose -f infra/docker-compose.prod.yml build --no-cache

echo "🚀 Starting containers..."
docker compose -f infra/docker-compose.prod.yml up -d

# Wait for services
echo "⏳ Waiting for services to start..."
sleep 15

# Run migrations
echo "🔄 Running database migrations..."
docker compose -f infra/docker-compose.prod.yml exec -T backend alembic upgrade head || echo "⚠️  Migrations may have failed, check logs"

# Show status
echo "📊 Service status:"
docker compose -f infra/docker-compose.prod.yml ps

echo "✅ Deployment complete!"
echo ""
echo "🌐 Services should be available at:"
echo "   - Backend API: http://${VPS_HOST}:8000"
echo "   - API Docs: http://${VPS_HOST}:8000/api/docs"
echo ""
echo "📝 Next steps:"
echo "   1. Configure nginx (if needed): sudo cp infra/nginx.conf /etc/nginx/sites-available/ai-job-intelligence"
echo "   2. Check logs: docker compose -f infra/docker-compose.prod.yml logs -f"

ENDSSH

# Cleanup
rm /tmp/ai-job-intelligence-deploy.tar.gz

echo "✨ Deployment script completed!"
