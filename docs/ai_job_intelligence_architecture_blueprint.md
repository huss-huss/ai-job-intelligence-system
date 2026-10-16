# 🧭 Architecture Blueprint

## AI Job Intelligence Automation System

**Document goal:** Provide a clear, implementation-ready architecture
blueprint (components, data flow, agent/workflow orchestration,
deployment topology, and key design decisions). This blueprint is
written to be **Cursor-friendly**: it explains *what to build* and *how
the parts talk to each other*.

------------------------------------------------------------------------

# 1) Architecture at a Glance

## 1.1 High-level Component Diagram (Mermaid)

``` mermaid
flowchart LR
  U[User] -->|HTTPS| FE[Frontend Dashboard<br/>Next.js]
  FE -->|REST/WS| API[Backend API<br/>FastAPI]

  API -->|enqueue jobs| Q[(Redis Queue)]
  Q --> W[Worker Service<br/>Scraper + Orchestrator]

  W -->|write| DB[(PostgreSQL)]
  W -->|embeddings| V[(pgvector in Postgres)]
  W -->|call| LLM[LLM Provider API]

  API -->|read| DB
  API -->|read/search| V

  W -->|notify| NTF[Notifications<br/>Email/Telegram/Webhook]
  API -->|logs/metrics| OBS[Observability<br/>Logs + Traces + Metrics]
  W -->|logs/metrics| OBS
```

------------------------------------------------------------------------

# 2) Deployment Topology (VPS)

## 2.1 Services (Docker Compose)

-   **nginx**: reverse proxy + TLS termination
-   **frontend**: Next.js app (static + SSR)
-   **backend**: FastAPI API service
-   **worker**: async tasks (scraping + AI processing + automation)
-   **redis**: queue + short-term cache
-   **postgres**: relational store + **pgvector** for embeddings
-   **observability (optional)**: loki/promtail + grafana, or just
    structured logs to files

## 2.2 Network & Ports

-   Public: **80/443** → Nginx
-   Internal:
    -   frontend: 3000
    -   backend: 8000
    -   redis: 6379
    -   postgres: 5432

## 2.3 Reverse Proxy Routes

-   `/` → Frontend
-   `/api/*` → Backend
-   `/health` → Backend health check

------------------------------------------------------------------------

# 3) Data Flow Blueprint

## 3.1 Core Pipeline

``` mermaid
sequenceDiagram
  autonumber
  participant S as Scheduler (Cron)
  participant API as Backend API (FastAPI)
  participant Q as Redis Queue
  participant W as Worker
  participant L as LLM API
  participant DB as Postgres
  participant N as Notifications

  S->>API: POST /api/scrape (source configs)
  API->>Q: enqueue SCRAPE_TASK (source_id, params)
  Q->>W: worker pulls SCRAPE_TASK
  W->>W: scrape & normalize jobs
  W->>DB: upsert raw jobs
  W->>Q: enqueue ANALYZE_TASK (job_ids)

  Q->>W: worker pulls ANALYZE_TASK
  W->>L: classify/extract fields (JSON schema)
  L-->>W: structured analysis
  W->>DB: save analysis + score
  W->>DB: store embeddings (pgvector)
  W->>N: alert if score >= threshold
```

------------------------------------------------------------------------

# 4) Agent & Workflow Orchestration Model

## 4.1 "Agent" in this System (No Buzzwords)

An **agent** here = an LLM-backed decision module that can: - choose
tools/actions (scrape, analyze, notify) - produce structured outputs -
follow multi-step workflows with state

We implement agent behavior via **workflow orchestration + tool
calling**, not a chatbot UI.

## 4.2 Workflow State Machine (Recommended)

Use a **stateful graph** (LangGraph) *or* a **deterministic workflow**
(your own task chain).

### Option A: Deterministic Task Chain (Simpler, reliable)

-   `SCRAPE_TASK` → `NORMALIZE` → `UPSERT_RAW` → `ANALYZE_TASK` →
    `SCORE` → `ALERT`
-   Pros: predictable, easier debugging, great for portfolio
-   Cons: less "autonomous" planning

### Option B: Graph Orchestration (More agentic)

Model states like: - `Collect` → `Filter` → `Enrich` → `Score` → `Act` -
Use a graph runner with explicit state transitions

## 4.3 Tool Calling Surface (What the "agent" can do)

Define tools as functions callable by the AI pipeline: -
`extract_structured_fields(job_text) -> JSON` -
`compute_score(structured_fields) -> float` -
`store_embedding(text) -> vector_id` -
`send_alert(payload) -> success` - (optional) `dedupe_and_merge(job)`

**Rule:** AI outputs must be validated against a JSON schema before any
action is taken.

------------------------------------------------------------------------

# 5) Key Modules & Boundaries

## 5.1 Backend API (FastAPI)

Responsibilities: - Authentication (later) - Expose endpoints for UI -
Trigger scrapes / analyses - Serve job listings, filters, and stats

Suggested endpoints: - `GET /api/health` - `POST /api/scrape` (manual
trigger) - `GET /api/sources` - `POST /api/sources` -
`GET /api/jobs?status=&source=&min_score=&q=&page=` -
`GET /api/jobs/{id}` - `GET /api/runs` (scrape/analyze runs) -
`GET /api/metrics` (optional)

## 5.2 Worker Service

Responsibilities: - Scheduled ingestion - Scraping + parsing +
normalization - AI analysis (LLM calls) - Scoring + routing decisions -
Notifications + webhooks

## 5.3 Data Layer (Postgres + pgvector)

Tables (minimum viable): - `sources` (id, name, base_url, config json,
enabled) - `jobs_raw` (id, source_id, url, title, company, location,
description, posted_at, hash, created_at) - `jobs_analysis` (job_id,
extracted_json, score, seniority, salary_min, salary_max, skills\[\],
created_at) - `embeddings` (id, job_id, embedding vector, model,
created_at) - `runs` (id, type scrape/analyze, source_id, status,
started_at, ended_at, stats json) - `alerts` (id, job_id, channel,
status, created_at)

------------------------------------------------------------------------

# 6) Scoring & Classification (Practical Rules)

## 6.1 Structured Extraction Schema (Example)

The LLM must return JSON like:

``` json
{
  "seniority": "junior|mid|senior|lead",
  "skills": ["python", "fastapi", "redis"],
  "salary": { "min": 0, "max": 0, "currency": "USD", "period": "hour|month|year" },
  "remote": true,
  "role_family": "backend|data|ml|devops|other",
  "summary": "short",
  "risk_flags": ["vague", "commission_only"]
}
```

## 6.2 Scoring (Deterministic Layer)

Compute a score using *rules* (not AI) so it's stable: - + points for
keywords matching your target niches - + points for remote /
contract-to-hire - + points for salary presence and threshold - - points
for low quality signals (vague, spam, duplicates)

**Why:** deterministic scoring is debuggable and improves trust.

------------------------------------------------------------------------

# 7) Observability & Reliability Blueprint

## 7.1 Logging

-   Structured JSON logs from backend and worker
-   Include: `run_id`, `source_id`, `task_id`, `job_id`
-   Log LLM request metadata: model, tokens, latency (never log secrets)

## 7.2 Retries & Backoff

-   Scraping: retries with exponential backoff
-   LLM calls: retry on rate limit/timeouts; circuit breaker if repeated
    failures
-   Queue: dead-letter pattern for failed tasks (store in DB `runs`
    stats)

## 7.3 Idempotency

-   Deduplicate jobs via `hash(url + title + company)`
-   Upserts only; tasks safe to rerun

------------------------------------------------------------------------

# 8) Security Blueprint (VPS-ready)

-   Store secrets in `.env` on VPS (later: Doppler/1Password)
-   Restrict DB/Redis to internal Docker network
-   Nginx TLS + HTTP security headers
-   Rate limit `/api/scrape` endpoints
-   Validate all AI outputs against schema

------------------------------------------------------------------------

# 9) CI/CD Blueprint (GitHub Actions)

Pipeline stages: 1. **Lint & Test** 2. **Build Docker images** 3.
**Deploy via SSH** 4. **Restart services** 5. **Health check**
(`/api/health`)

Deployment strategy: -
`docker compose pull && docker compose up -d --remove-orphans` - Keep
`.env` on VPS (not in repo) - Use GitHub Secrets for SSH key + host/user

------------------------------------------------------------------------

# 10) MVP vs V1 Scope (So You Don't Overbuild)

## MVP (portfolio-ready)

-   1--2 sources scraping
-   Store raw jobs + analyzed fields
-   Deterministic scoring
-   Dashboard list view + filters
-   Alerts (email/webhook)
-   Docker deploy on VPS + CI/CD

## V1 (upgrade)

-   Multiple sources + plugin system
-   User accounts + saved searches
-   Vector search UI (semantic search)
-   Multi-agent workflow graph + tool routing
-   Analytics dashboard (trend reports)

------------------------------------------------------------------------

# 11) "What to show" for Portfolio (Screenshots Checklist)

-   Architecture diagram (this doc)
-   Dashboard job list + filters
-   "Run history" page showing scrape/analyze runs
-   Logs snippet showing pipeline success
-   Notification screenshot (email/webhook payload)

------------------------------------------------------------------------

# ✅ Final Notes

This blueprint is designed to make your project feel like a **real
production system**: - clear module boundaries - deterministic
reliability - agent workflow framed as orchestration + tool calling -
deployable and maintainable on a VPS

Use this as the reference architecture while implementing features from
the SRS.
