# Testing & Verification Guide

Use this checklist to verify the system works correctly before a client demo or deployment.

---

## 1. Quick smoke test (using the UI)

Do this with the app running locally (or on your deployed URL).

### Step 1: Health and dashboard load

- Open the dashboard (e.g. http://localhost:3000).
- You should see: header **AI Job Intelligence**, **Filters**, **Sources**, **Run history**.
- If the page loads and shows these sections, the frontend and API connection are working.

### Step 2: Trigger a scrape from the UI

- In **Sources**, you have at least one source (e.g. "Test Board").
- Click **Scrape** next to that source (or **Scrape all**).
- Nothing should error in the UI; the button may briefly show “Enqueuing…” or similar.

### Step 3: Check Run history (proof the pipeline ran)

- In **Run history**, wait a few seconds and refresh if needed.
- You should see **new rows** after your click:
  - At least one **Scrape** run with status **completed** (green check).
  - Stats like `{"scraped": 1, "inserted": 1}` or `{"scraped": 1, "inserted": 0}` (0 is normal if that job already existed).
- If you had new jobs inserted, you should then see an **Analyze** run with status **completed** and something like `{"processed": 1}`.

**What “correct” looks like:**

- Scrape run: **completed** and `scraped` ≥ 1.
- If any new jobs were inserted: an Analyze run **completed** with `processed` ≥ 1.
- No runs stuck in **running** for a long time (e.g. > 1–2 minutes) and no **failed** runs for normal use.

**Why you might only see "Scrape completed":** Analyze runs only when a scrape **inserts new jobs**. If the scraper returns the same job (e.g. the sample placeholder), you get `inserted: 0` and no new Analyze run. Use **Re-analyze all jobs** in the UI to run analysis on existing jobs and see "Analyze completed" in run history.

#### Step 3b: Verify analyze (Re-analyze jobs)

- In the **Re-analyze jobs** card, click **Re-analyze all jobs**.
- In **Run history** you should see an **Analyze** run (status **running** briefly, then **completed** with e.g. `{"processed": 1}`). That confirms the AI analysis pipeline works.

### Step 4: Check job list and scores

- In the main area, you should see **“X job(s) (page 1 of …)”**.
- At least one **job card** with:
  - Title, company, location (if present).
  - A **score %** (e.g. 65%) in the top-right of the card — this is the AI opportunity score.
  - Tags like **Junior**, **Playwright**, **scraping** (or similar) from the analyzer.
- Click the job title: it should open the job URL in a new tab (or show the link target).

**What “correct” looks like:**

- Jobs appear after a scrape.
- Each analyzed job has a score and at least some tags or seniority.
- Links open the expected URL.

### Step 5: Filters (optional but recommended)

- Change **All sources** to the only source (e.g. Test Board): list should stay correct.
- Set **Any score** to **70%+** or **50%+**: list should filter by score (or show empty if none match).
- Type in **Search title or company…**: list should filter by that text.

If all of the above behave as described, the UI path and pipeline are working as intended for a client demo.

---

## 2. What each part of the system is doing (for your own confidence)

| What you see in the UI | What it proves |
|------------------------|----------------|
| **Run history: Scrape completed** with `scraped` / `inserted` | Worker received the task, ran the scraper, and wrote to the DB. |
| **Run history: Analyze completed** with `processed` | Worker ran the AI analysis and saved scores/skills/seniority. |
| **Job cards with a score %** | Jobs were analyzed and the score is stored and served by the API. |
| **Filters (source, score, search)** | API and frontend are correctly filtering and paginating jobs. |

So: **Scrape completed + Analyze completed + jobs with scores + filters working** = pipeline and UI are working correctly.

---

## 3. Optional: API checks (for deployment or scripting)

You can call the API directly to double-check.

- **Health**  
  `GET /api/health`  
  Expect: `{"status":"ok","service":"api"}`

- **Jobs**  
  `GET /api/jobs`  
  Expect: `{"items": [...], "total": N, "page": 1, ...}` with at least one item that has `analysis.score` after a scrape + analyze.

- **Runs**  
  `GET /api/runs`  
  Expect: list of runs with `type`, `status`, `stats` (e.g. `scrape`/`analyze`, `completed`, `{"scraped":1,"inserted":1}`).

Example (replace with your base URL when deployed):

```bash
curl -s http://localhost:8000/api/health
curl -s http://localhost:8000/api/jobs | head -c 500
curl -s http://localhost:8000/api/runs | head -c 500
```

---

## 4. Before a client demo

1. Run through **Section 1** once (scrape → check run history → check job list and filters).
2. Confirm there are no **failed** runs and at least one job with a **score**.
3. If you deploy to a live URL, run the same steps on that URL and, if you want, the **Section 3** API checks against the deployed base URL.

That is enough to say with confidence: “The system is working: scrapes run, analysis runs, scores and filters work in the UI.”
