# AI Job Intelligence Automation System

Production-style AI automation: scrape job listings → queue → AI analysis → database → API → dashboard.

**Stack:** FastAPI, RQ (Redis), PostgreSQL + pgvector, Next.js, Tailwind, ShadCN-style UI.

---

## Repository structure

```
ai-job-intelligence-system/
├── backend/          # FastAPI API, DB models, queue client
├── worker/           # RQ tasks: scraper + AI analysis + scoring
├── frontend/         # Next.js dashboard
├── infra/            # Docker Compose, .env.example
└── docs/             # SRS, architecture blueprint, deployment guide
```

---

## Local development (no Docker)

### 1. Environment

```bash
cp infra/.env.example .env
# Edit .env: set OPENAI_API_KEY if you want LLM extraction (otherwise heuristics are used).
```

### 2. Virtual environment (recommended)

Using a **Python virtual environment** keeps backend and worker dependencies isolated and reproducible. Alternatives (e.g. global `pip`, Conda) work too, but a venv is the standard for local Python projects.

```bash
# From repo root
python3 -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r backend/requirements.txt -r worker/requirements.txt
```

Use this same activated venv for running the backend, worker, and Alembic. When you’re done, `deactivate` to leave the venv.

### 3. PostgreSQL + Redis

Start Postgres (with pgvector) and Redis locally, or use Docker for just the data layer:

```bash
docker compose -f infra/docker-compose.yml up -d postgres redis
```

Then run migrations (with your venv activated if you use one):

```bash
cd backend
alembic upgrade head
```

### 4. Backend

```bash
cd backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

API: http://localhost:8000 — Docs: http://localhost:8000/api/docs

### 5. Worker

From repo root (venv activated). RQ needs repo root on path so `worker.tasks` resolves:

```bash
# On macOS, set this if the worker crashes when running scrape (Playwright + fork):
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
PYTHONPATH=backend:. rq worker default --path .
```

**Important:** If you see "Python stopped working unexpectedly" or scraping hangs:
- Make sure `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` is set before starting the worker
- Check worker logs for error messages (they'll appear in the terminal where you started the worker)
- Verify Redis is running: `redis-cli ping` should return `PONG`
- Check Run history in the UI - failed runs will show error details

### 6. Frontend

```bash
cd frontend
npm install
npm run dev
```

Dashboard: http://localhost:3000 — Uses Next.js rewrites to proxy `/api/*` to the backend (port 8000).

---

## Run full stack with Docker

```bash
cp infra/.env.example .env
docker compose -f infra/docker-compose.yml up -d --build
```

- API: http://localhost:8000  
- Frontend: run locally (`cd frontend && npm run dev`) and set `NEXT_PUBLIC_API_URL=http://localhost:8000` if needed (rewrites point to localhost:8000 by default).

---

## API overview

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | /api/health | Health check |
| GET | /api/sources | List sources |
| POST | /api/sources | Create source |
| POST | /api/sources/{id}/scrape | Enqueue scrape for one source |
| POST | /api/scrape | Enqueue scrape (all or body `source_id`) |
| GET | /api/jobs | List jobs (filter: source, min_score, q, page) |
| GET | /api/jobs/{id} | Job detail |
| GET | /api/runs | List scrape/analyze runs |

---

## Adding a job source

```bash
curl -X POST http://localhost:8000/api/sources \
  -H "Content-Type: application/json" \
  -d '{"name": "Example Board", "base_url": "https://example.com/jobs", "enabled": true}'
```

Then trigger a scrape from the dashboard or:

```bash
curl -X POST http://localhost:8000/api/scrape -H "Content-Type: application/json" -d '{}'
```

---

## Docs

- [PROJECT_HANDOFF.md](PROJECT_HANDOFF.md) — **Detailed handoff: what was built (for collaborators)**
- [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) — Project idea & overview (for clients; recreate if missing)
- [docs/ai_job_intelligence_SRS.md](docs/ai_job_intelligence_SRS.md) — Requirements
- [docs/ai_job_intelligence_architecture_blueprint.md](docs/ai_job_intelligence_architecture_blueprint.md) — Architecture and data flow
- [DEPLOYMENT.md](DEPLOYMENT.md) — VPS deploy and Docker
- [COMPLETE_SETUP.md](COMPLETE_SETUP.md) — Firewall, Postgres password, CI/CD
- [docs/TESTING_AND_VERIFICATION.md](docs/TESTING_AND_VERIFICATION.md) — Testing
- [docs/PRODUCTION_TEST_PLAN.md](docs/PRODUCTION_TEST_PLAN.md) — Production test plan
