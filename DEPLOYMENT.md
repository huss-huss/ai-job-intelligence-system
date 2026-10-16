# 🚀 Deployment Guide

This guide covers deploying the AI Job Intelligence System to your VPS.

## Prerequisites

- VPS with Ubuntu 22.04+ (or similar Linux distribution)
- Docker and Docker Compose installed
- SSH access to the VPS
- Domain name (optional, for production)

## Quick Deploy

### Option 1: Using Deployment Script (Recommended)

```bash
# From your local machine (repo root)
export VPS_USER=hussen
export VPS_HOST=<your-server-ip>
./scripts/deploy.sh
```

### Option 2: Manual Deployment

```bash
# 1. SSH into your VPS
ssh <user>@<your-server-ip>

# 2. Clone or upload the repository
cd ~
git clone <your-repo-url> ai-job-intelligence-system
# OR upload files via SCP

# 3. Navigate to project directory
cd ai-job-intelligence-system

# 4. Create .env file
cp infra/.env.example .env
nano .env  # Edit with your configuration

# 5. Build and start services
docker compose -f infra/docker-compose.prod.yml up -d --build

# 6. Run migrations
docker compose -f infra/docker-compose.prod.yml exec backend alembic upgrade head

# 7. Check status
docker compose -f infra/docker-compose.prod.yml ps
```

## Environment Variables

Create a `.env` file in the project root with:

```env
# Database
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_secure_password
POSTGRES_DB=jobs

# Redis
REDIS_URL=redis://redis:6379/0

# OpenAI (required for AI analysis)
OPENAI_API_KEY=your_openai_api_key
OPENAI_MODEL=gpt-4o-mini

# Optional: Alerts
ALERT_WEBHOOK_URL=
ALERT_SCORE_THRESHOLD=0.7
```

## CI/CD Setup (GitHub Actions)

### 1. Add GitHub Secrets

Go to your GitHub repository → Settings → Secrets and variables → Actions, and add:

- `VPS_SSH_KEY`: Your private SSH key for VPS access
- `VPS_HOST`: `<your-server-ip>` (your VPS IP)
- `VPS_USER`: `hussen`
- `VPS_PATH`: `~/ai-job-intelligence-system` (optional)

### 2. Generate SSH Key (if needed)

```bash
# On your local machine
ssh-keygen -t ed25519 -C "github-actions" -f ~/.ssh/vps_deploy_key

# Copy public key to VPS
ssh-copy-id -i ~/.ssh/vps_deploy_key.pub <user>@<your-server-ip>

# Add private key to GitHub Secrets (copy contents of ~/.ssh/vps_deploy_key)
```

### 3. Push to Main Branch

The CI/CD pipeline will automatically deploy when you push to `main` or `master`:

```bash
git add .
git commit -m "Deploy to production"
git push origin main
```

## Nginx Setup (Optional - for production domain)

### 1. Install Nginx

```bash
sudo apt update
sudo apt install nginx -y
```

### 2. Configure Nginx

```bash
# Copy configuration
sudo cp infra/nginx.conf /etc/nginx/sites-available/ai-job-intelligence

# Edit configuration (replace domain name)
sudo nano /etc/nginx/sites-available/ai-job-intelligence

# Enable site
sudo ln -s /etc/nginx/sites-available/ai-job-intelligence /etc/nginx/sites-enabled/

# Test configuration
sudo nginx -t

# Reload nginx
sudo systemctl reload nginx
```

### 3. Build Frontend for Production

```bash
# On VPS or locally
cd frontend
npm install
npm run build

# Copy build output to nginx directory
sudo mkdir -p /var/www/ai-job-intelligence/frontend
sudo cp -r out/* /var/www/ai-job-intelligence/frontend/
```

## SSL/HTTPS Setup (Let's Encrypt)

```bash
# Install certbot
sudo apt install certbot python3-certbot-nginx -y

# Get certificate
sudo certbot --nginx -d yourdomain.com

# Auto-renewal is set up automatically
```

## Monitoring & Logs

### View Logs

```bash
# All services
docker compose -f infra/docker-compose.prod.yml logs -f

# Specific service
docker compose -f infra/docker-compose.prod.yml logs -f backend
docker compose -f infra/docker-compose.prod.yml logs -f worker
```

### Check Service Status

```bash
docker compose -f infra/docker-compose.prod.yml ps
```

### Restart Services

```bash
docker compose -f infra/docker-compose.prod.yml restart
```

## Troubleshooting

### Services won't start

```bash
# Check logs
docker compose -f infra/docker-compose.prod.yml logs

# Check Docker status
docker ps -a
systemctl status docker
```

### Database connection issues

```bash
# Check PostgreSQL logs
docker compose -f infra/docker-compose.prod.yml logs postgres

# Test connection
docker compose -f infra/docker-compose.prod.yml exec backend python -c "from app.database import engine; print('Connected!')"
```

### Worker not processing jobs

```bash
# Check worker logs
docker compose -f infra/docker-compose.prod.yml logs worker

# Check Redis connection
docker compose -f infra/docker-compose.prod.yml exec redis redis-cli ping
```

## Backup

### Database Backup

```bash
# Create backup
docker compose -f infra/docker-compose.prod.yml exec postgres pg_dump -U postgres jobs > backup_$(date +%Y%m%d).sql

# Restore backup
docker compose -f infra/docker-compose.prod.yml exec -T postgres psql -U postgres jobs < backup_20240220.sql
```

## Update Deployment

```bash
# Pull latest code
git pull

# Rebuild and restart
docker compose -f infra/docker-compose.prod.yml up -d --build

# Run migrations if needed
docker compose -f infra/docker-compose.prod.yml exec backend alembic upgrade head
```

## Security Checklist

- [ ] Change default PostgreSQL password
- [ ] Use strong passwords in `.env`
- [ ] Set up firewall (UFW)
- [ ] Enable SSL/HTTPS
- [ ] Restrict SSH access (key-based only)
- [ ] Keep system updated: `sudo apt update && sudo apt upgrade`
- [ ] Regular backups
- [ ] Monitor logs for suspicious activity
