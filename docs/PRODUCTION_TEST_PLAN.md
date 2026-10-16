# Production Test Plan - Complete Feature Verification

This guide walks through testing all features of the AI Job Intelligence System in a production-like scenario.

## Prerequisites

✅ Backend running on `http://localhost:8000`  
✅ Worker running (with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` on macOS)  
✅ Frontend running on `http://localhost:3000`  
✅ PostgreSQL and Redis running (via Docker)

---

## Test Sequence

### Phase 1: Clean State & Initial Setup

1. **Clear existing data** (start fresh)
   - Open dashboard: `http://localhost:3000`
   - Click **"Clear jobs"** button (in Filters card header)
   - Confirm deletion
   - ✅ Verify: Job list should be empty immediately

2. **Verify Sources**
   - Check **Sources** card shows at least one source
   - If none, create one via API:
     ```bash
     curl -X POST http://localhost:8000/api/sources \
       -H "Content-Type: application/json" \
       -d '{"name": "Test Board", "base_url": "https://example.com/jobs", "enabled": true}'
     ```

---

### Phase 2: Create Test Data

3. **Create Test Jobs**
   - Click **"Create test jobs"** button (in Test data card)
   - ✅ Verify: Button shows "Creating..." with spinner
   - ✅ Verify: Message appears: "✓ Created X job(s). Analyzing..."
   - ✅ Verify: Jobs appear in the list immediately (without refresh)
   - Wait for analysis to complete (~10-30 seconds)
   - ✅ Verify: Message updates to "✓ Created and analyzed X job(s). Analysis complete!"
   - ✅ Verify: Jobs now show analysis scores (e.g., 65%, 72%)

---

### Phase 3: Job Display & Features

4. **Verify Job Cards**
   - ✅ Each job card shows:
     - Title (clickable link)
     - Company name
     - Location
     - Score percentage (top-right, colored badge)
     - Seniority tag (e.g., "Junior", "Mid", "Senior")
     - Skills tags (e.g., "Python", "React")
   - ✅ Click job title → opens URL in new tab
   - ✅ Score colors: Green (high), Yellow (medium), Red (low)

5. **Verify Pagination**
   - If more than 20 jobs, check pagination controls
   - ✅ Click "Next" → shows next page
   - ✅ Click "Previous" → shows previous page
   - ✅ Page numbers update correctly

---

### Phase 4: Filtering Features

6. **Test Source Filter**
   - Select a source from dropdown (e.g., "Test Board")
   - ✅ Verify: List filters to show only jobs from that source
   - Select "All sources"
   - ✅ Verify: Shows all jobs again

7. **Test Score Filter**
   - Set filter to "70%+"
   - ✅ Verify: Only shows jobs with score ≥ 70%
   - Set filter to "50%+"
   - ✅ Verify: Shows jobs with score ≥ 50%
   - Set back to "Any score"
   - ✅ Verify: Shows all jobs

8. **Test Search Filter**
   - Type "Python" in search box
   - ✅ Verify: Filters jobs by title/company containing "Python"
   - Clear search
   - ✅ Verify: Shows all jobs again
   - Try searching for a company name
   - ✅ Verify: Filters correctly

9. **Test Combined Filters**
   - Set source filter + score filter + search
   - ✅ Verify: All filters work together correctly

---

### Phase 5: Scraping Feature

10. **Test Individual Source Scrape**
    - Click **"Scrape"** button next to a source
    - ✅ Verify: Button shows "Processing..." with spinner
    - ✅ Verify: Run history shows new "Scrape" run (status: running → completed)
    - ✅ Verify: If new jobs found, they appear in the list automatically
    - ✅ Verify: If no new jobs (deduplication), list stays the same

11. **Test Scrape All**
    - Click **"Scrape all"** button
    - ✅ Verify: Button shows "Processing scrape task…"
    - ✅ Verify: Run history shows scrape runs for all enabled sources
    - ✅ Verify: Jobs list refreshes automatically when scraping completes

---

### Phase 6: Analysis Feature

12. **Test Re-analyze Jobs**
    - Click **"Re-analyze all jobs"** button
    - ✅ Verify: Button shows "Analyzing..." with spinner
    - ✅ Verify: Status updates: "Enqueuing..." → "Processing..." → "Completed"
    - ✅ Verify: Run history shows new "Analyze" run (status: completed)
    - ✅ Verify: Job scores update automatically when analysis completes
    - ✅ Verify: Skills and seniority tags update

---

### Phase 7: Run History

13. **Verify Run History**
    - Check **Run history** card
    - ✅ Verify: Shows recent runs (scrape and analyze)
    - ✅ Verify: Each run shows:
       - Type (scrape/analyze)
       - Status (completed/running/failed) with icon
       - Timestamp
       - Stats (e.g., `{"scraped": 8, "inserted": 8}`)
    - ✅ Verify: Completed runs show green checkmark
    - ✅ Verify: Failed runs show red X (if any)
    - ✅ Verify: Run history auto-updates when new runs complete

14. **Test Clear Run History**
    - Click **"Clear"** button (trash icon) in Run history header
    - ✅ Verify: Confirmation dialog appears
    - Confirm deletion
    - ✅ Verify: Run history clears immediately
    - ✅ Verify: Button disappears when no runs exist

---

### Phase 8: Error Handling

15. **Test Error Scenarios** (if applicable)
    - Stop the worker temporarily
    - Try scraping → should show error message
    - ✅ Verify: Error message appears in UI
    - ✅ Verify: Error suggests checking worker/Redis
    - Restart worker and try again
    - ✅ Verify: Works normally after restart

---

### Phase 9: Data Management

16. **Test Clear Jobs**
    - Click **"Clear jobs"** button
    - ✅ Verify: Confirmation dialog appears
    - Confirm deletion
    - ✅ Verify: All jobs disappear immediately
    - ✅ Verify: Job list shows "No jobs found"
    - ✅ Verify: Run history still shows (not cleared)

17. **Re-create Test Data**
    - Click **"Create test jobs"** again
    - ✅ Verify: Creates new jobs successfully
    - ✅ Verify: Analysis runs automatically
    - ✅ Verify: All features work again

---

## Success Criteria

✅ **All features work without page refresh:**
- Jobs appear/disappear automatically
- Analysis scores update automatically
- Run history updates automatically
- Filters work in real-time

✅ **All UI elements function correctly:**
- Buttons show loading states
- Error messages display properly
- Success messages appear and disappear
- Pagination works smoothly

✅ **Data integrity:**
- Jobs are saved correctly
- Analysis scores are calculated and stored
- Run history tracks all operations
- Filters return correct results

✅ **Performance:**
- UI remains responsive during operations
- No hanging or frozen states
- Operations complete within reasonable time

---

## Quick Verification Checklist

- [ ] Dashboard loads without errors
- [ ] Test jobs can be created
- [ ] Jobs display with all fields (title, company, score, tags)
- [ ] Filters work (source, score, search)
- [ ] Pagination works
- [ ] Scraping works (individual and all)
- [ ] Analysis works and updates scores
- [ ] Run history shows all operations
- [ ] Clear functions work (jobs and runs)
- [ ] UI updates automatically (no manual refresh needed)
- [ ] Error handling works gracefully

---

## Notes

- If any test fails, check:
  1. Backend logs (terminal where uvicorn is running)
  2. Worker logs (terminal where rq worker is running)
  3. Browser console (F12 → Console tab)
  4. Run history for failed runs with error details

- For production deployment, run this same test sequence on the deployed URL.
