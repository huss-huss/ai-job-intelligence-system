#!/bin/bash
# Script to update PostgreSQL password
# Run from VPS: cd ~/ai-job-intelligence-system && ./scripts/update-postgres-password.sh <new_password>
# Or without password to auto-generate: ./scripts/update-postgres-password.sh

set -e

cd ~/ai-job-intelligence-system

if [ -z "$1" ]; then
  # Generate secure password
  NEW_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-25)
  echo "🔐 Generated secure password: $NEW_PASSWORD"
else
  NEW_PASSWORD="$1"
  echo "🔐 Using provided password"
fi

echo ""
echo "⚠️  This will:"
echo "   1. Update POSTGRES_PASSWORD in .env file"
echo "   2. Update DATABASE_URL in .env file"
echo "   3. Restart PostgreSQL container (data will be reset!)"
echo ""
echo "Press Ctrl+C to cancel, or Enter to continue..."
read

# Update .env file
echo "📝 Updating .env file..."
sed -i "s/POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$NEW_PASSWORD/" .env

# Update DATABASE_URL in .env (for localhost connections)
sed -i "s|DATABASE_URL=postgresql.*@localhost|DATABASE_URL=postgresql+asyncpg://postgres:$NEW_PASSWORD@localhost:5432/jobs|" .env

# Restart postgres with new password
echo "🔄 Restarting PostgreSQL with new password..."
docker-compose -f infra/docker-compose.prod.yml stop postgres
docker volume rm infra_postgres_data 2>/dev/null || true
docker-compose -f infra/docker-compose.prod.yml up -d postgres

echo "⏳ Waiting for PostgreSQL to start..."
sleep 15

# Run migrations
echo "🔄 Running database migrations..."
docker-compose -f infra/docker-compose.prod.yml exec -T backend alembic upgrade head || echo "Migrations will run on next backend restart"

# Restart backend and worker
echo "🔄 Restarting backend and worker..."
docker-compose -f infra/docker-compose.prod.yml restart backend worker

echo ""
echo "✅ PostgreSQL password updated successfully!"
echo "📝 Password saved in .env file"
echo ""
echo "⚠️  Important:"
echo "   - Update GitHub secrets if using CI/CD"
echo "   - Keep .env file secure (don't commit to git)"
