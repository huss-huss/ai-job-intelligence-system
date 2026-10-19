# FirstFetch — AI Job Intelligence

[![CI](https://github.com/huss-huss/ai-job-intelligence-system/actions/workflows/ci.yml/badge.svg)](https://github.com/huss-huss/ai-job-intelligence-system/actions/workflows/ci.yml)

**Live demo:** [firstfetch-eight.vercel.app](https://firstfetch-eight.vercel.app) (read-only; refreshed daily by Vercel Cron) · API: [firstfetch-api.vercel.app/api/docs](https://firstfetch-api.vercel.app/api/docs)

FirstFetch pulls listings from public job APIs, scores each one against **your** profile with an explainable 0–100 score, and sends the strong matches to Notion.

I built it because job boards rank by recency, not fit, and most "remote" listings quietly require living in the US. FirstFetch reads the location and work-authorization rules first, so jobs you can't apply for never reach the top of the list.

```text
$ python -m worker.cli discover --top 3
Score  Elig  Title                                             Company                 Source
----------------------------------------------------------------------------------------------------
   52  ?     Senior .NET Software Engineer                     OkWhen                  remoteok
             Title matches target role 'software engineer' · Skills: typescript, aws · Seniority fits (senior) · ...
   51  ?     Senior Backend Engineer Build AI Agents           Salesforge              remoteok
             Title matches target role 'backend' · Skills: agents · Seniority fits (senior) · Location not specified
   47  ?     Senior React Full stack Developer                 Lemon.io                remoteok
             Target role mentioned in description, not title · Skills: python, llm, agents, node, typescript ...
----------------------------------------------------------------------------------------------------
117 fetched · 115 unique · 57 not open to your location · 0 LLM calls (0 tokens)
```
<sub>Real output from 2026-10-16 using the rules-only extractor (no API key).</sub>

## How it works

```text
 sources ──► fetch ──► dedupe ──► rules extract ──► pre-score ──► LLM extract* ──► score ──► Postgres / Notion / webhook
 (6 APIs +   (retry,   (per source   (skills,          (cheap)       (*only jobs      (0-100 with
  Playwright) throttle) + across      seniority,                       that pre-score   reasons)
                        boards)       salary, risks)                   ≥ 35)
```

- **Sources** (`worker/sources/`): Remotive, RemoteOK, Arbeitnow, Himalayas, any Greenhouse or Lever careers board, plus an opt-in Playwright scraper for sites without an API. All requests go through one HTTP client with throttling, exponential backoff with jitter, and `Retry-After` support.
- **Extraction** (`worker/ai.py`): keyword rules by default. With `OPENAI_API_KEY`, or `LLM_BASE_URL` pointing at any OpenAI-compatible server such as Ollama, a strict JSON schema is sent and the reply is validated with Pydantic. Any failure falls back to rules instead of failing the run. Token usage is recorded per job.
- **Scoring** (`worker/matching.py`): deterministic, so it's testable and auditable. The LLM supplies facts; it doesn't decide the score.

  | Component | Points | Notes |
  |---|---|---|
  | Role | 25 | target title in the job title (5 if only in the description) |
  | Skills | 35 | weighted coverage of your top skills; halved for off-target titles |
  | Location | 15 | "USA only", work-authorization clauses, and on-site roles make a job **ineligible** (score capped at 20) |
  | Seniority | 10 | |
  | Salary | 10 | top of the range, normalized to annual USD, against `min_salary_usd` (5 when unknown) |
  | Recency | 5 | |
  | Risk flags | −10 each | commission-only, unpaid, upfront fees, vague postings |

- **Salary normalization** (`worker/salary.py`): hourly × 2080 (40 h × 52 weeks), daily × 260, weekly × 52, monthly × 12; no period means annual and no currency means USD. Other currencies convert with a dated static table (USD per unit), which `FX_RATES_JSON='{"EUR": 1.1}'` overrides or extends. A currency or period the table doesn't know is scored as unknown, never guessed. Period and currency are read from the source's fields or from text such as `$40/hr`, `€5,000 per month`, `£70k`, `USD 120,000 - 150,000`. The reason shows both figures, e.g. `Salary up to €60,000/yr ≈ $70,200`. Stored `salary_min`/`salary_max` stay in the source's own unit.

- **Notion** (`worker/notion.py`): upserts keyed by job URL, so re-runs update scores without duplicating pages. The `Status` you set in Notion is never overwritten.

**Stack:** Python 3.12, FastAPI, SQLAlchemy + Alembic, PostgreSQL (pgvector), Redis + RQ, httpx, Pydantic, Next.js dashboard, Docker Compose, GitHub Actions.

## Quick start (no database)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt -r worker/requirements.txt
cp profile.example.yaml profile.yaml        # edit titles, skills, location, salary floor
python -m worker.cli discover --top 15
python -m worker.cli discover --sources sources.example.yaml --json results.json
```

### Notion

1. Create an internal integration at https://www.notion.so/my-integrations and share a database with it.
2. Set `NOTION_TOKEN` and `NOTION_DATABASE_ID` in `.env`.
3. `python -m worker.cli notion-setup` adds the properties FirstFetch writes.
4. `python -m worker.cli discover --notion` syncs eligible jobs at or above `notify_threshold`.

## Tests

```bash
pip install pytest ruff
pytest          # 144 tests, offline: adapters run against trimmed real API responses
ruff check worker tests
```

---

## Local development (no Docker)

### 1. Environment

```bash
cp infra/.env.example .env
# Optional: OPENAI_API_KEY (or LLM_BASE_URL for a local model), NOTION_TOKEN, NOTION_DATABASE_ID
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

`config.type` picks the adapter; the other keys are adapter options (see `sources.example.yaml`).

```bash
curl -X POST http://localhost:8000/api/sources \
  -H "Content-Type: application/json" \
  -d '{"name": "Remotive · Python", "base_url": "https://remotive.com", "config": {"type": "remotive", "search": "python"}}'
```

Sources created before adapters existed (no `config.type`) keep working through the Playwright `html` adapter.

Then trigger a scrape from the dashboard or:

```bash
curl -X POST http://localhost:8000/api/scrape -H "Content-Type: application/json" -d '{}'
```

---

## Deploying to Vercel

Two Vercel projects plus a Neon Postgres database (free tiers are enough):

| Project | Root | What runs |
|---|---|---|
| API | repo root | `api/index.py` serves the FastAPI app as a Python function; `vercel.json` schedules `GET /api/cron/discover` daily |
| Dashboard | `frontend/` | static Next.js export; `frontend/vercel.json` proxies `/api/*` to the API project |

```bash
vercel link --project firstfetch-api            # from the repo root
vercel integration add neon                     # provisions Postgres and sets DATABASE_URL
DATABASE_URL=<unpooled url> alembic -c backend/alembic.ini upgrade head
vercel env add QUEUE_MODE production            # inline: no Redis/RQ worker on serverless
vercel env add CRON_SECRET production           # Vercel Cron sends it as a Bearer token
vercel env add ADMIN_TOKEN production           # makes the public deployment read-only
vercel deploy --prod
cd frontend && vercel link --project firstfetch && vercel deploy --prod
```

Writes against a deployment with `ADMIN_TOKEN` need the header `X-Admin-Token: <token>`.

## Limitations and next steps

- RemoteOK and Himalayas return shortened descriptions, so skill matches on those sources are under-counted. Fetching the full posting page is the next improvement.
- Salary conversion uses fixed assumptions: hourly rates are treated as full-time (2080 h/yr, no unpaid leave), 13th-month pay is ignored, and exchange rates come from a static table dated in `worker/salary.py` (override with `FX_RATES_JSON`). A bare `$` is read as USD, so CAD/AUD listings that write only `$` are slightly over-valued.
- Salary text without a period marker is taken as annual, so an unlabeled monthly figure ("€5,000") under-scores; currencies outside the table count as unknown.
- On Vercel each discovery runs inside one function call (up to 300 s) and the Hobby plan allows one cron run per day. Docker Compose with the RQ worker has neither limit.
- The pgvector `embeddings` table exists but isn't used yet. Semantic similarity against a CV is planned as an extra scoring component.
- Respect each API's terms: Remotive asks for a few requests per day, and RemoteOK and Arbeitnow ask for links back to listings.

## Docs

- [docs/ai_job_intelligence_SRS.md](docs/ai_job_intelligence_SRS.md): requirements
- [docs/ai_job_intelligence_architecture_blueprint.md](docs/ai_job_intelligence_architecture_blueprint.md): architecture and data flow
- [docs/TESTING_AND_VERIFICATION.md](docs/TESTING_AND_VERIFICATION.md): testing
