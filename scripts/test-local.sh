#!/usr/bin/env bash
# Test the project locally with venv.
# Prereqs: Docker running (for postgres + redis). From repo root:
#   ./scripts/test-local.sh

set -e
cd "$(dirname "$0")/.."
ROOT="$PWD"

# 1) Venv
if [ ! -d ".venv" ]; then
  echo "Creating venv..."
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r backend/requirements.txt -r worker/requirements.txt

# 2) Env file (backend loads .env from cwd when we run from repo root)
if [ ! -f ".env" ]; then
  cp infra/.env.example .env
  echo "Created .env from infra/.env.example"
fi

# 3) Postgres + Redis (Docker)
echo "Starting Postgres and Redis (Docker)..."
docker compose -f infra/docker-compose.yml up -d postgres redis
echo "Waiting for Postgres and Redis (up to 30s)..."
for i in $(seq 1 15); do
  if python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(1)
try:
  s.connect(('localhost', 5432))
  s.close()
  exit(0)
except: exit(1)
" 2>/dev/null; then
    if python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(1)
try:
  s.connect(('localhost', 6379))
  s.close()
  exit(0)
except: exit(1)
" 2>/dev/null; then
      break
    fi
  fi
  sleep 2
done
python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2)
try:
  s.connect(('localhost', 5432))
  s.close()
except Exception as e:
  print('Postgres not ready. Is Docker running? docker compose -f infra/docker-compose.yml up -d postgres redis')
  exit(1)
" || exit 1

# 4) Migrations
echo "Running migrations..."
cd backend && alembic upgrade head && cd ..

# 5) Backend (background; run from repo root so .env is loaded)
echo "Starting backend on :8000..."
PYTHONPATH=backend python -m uvicorn main:app --host 0.0.0.0 --port 8000 --app-dir backend &
BACKEND_PID=$!
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -s http://localhost:8000/api/health >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -s http://localhost:8000/api/health | head -1
echo ""

# 6) Worker (background; repo root on path so worker.tasks resolves)
# On macOS, avoid fork safety crash when worker uses Playwright:
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
echo "Starting RQ worker..."
PYTHONPATH=backend:. rq worker default --path . &
WORKER_PID=$!
sleep 2

# 7) Quick API test
echo "Testing API..."
curl -s http://localhost:8000/api/jobs | head -c 200
echo ""
echo "Backend PID: $BACKEND_PID  Worker PID: $WORKER_PID"
echo "API: http://localhost:8000  Docs: http://localhost:8000/api/docs"
echo "To run frontend: cd frontend && npm install && npm run dev  # then open http://localhost:3000"
echo "To stop: kill $BACKEND_PID $WORKER_PID"
