"""Cross-source dedupe: the same job seen on FreeHire, Djinni and We Work Remotely becomes one posting."""

import json
from dataclasses import replace
from pathlib import Path

from worker.pipeline import canonical_url, dedupe_across_sources, merge_postings
from worker.sources.base import JobPosting
from worker.sources.feeds import DjinniAdapter, WeWorkRemotelyAdapter
from worker.sources.freehire import FreeHireAdapter
from worker.sources.rss import parse_rss

FIXTURES = Path(__file__).parent / "fixtures"
FREEHIRE = [FreeHireAdapter.parse(j) for j in json.loads((FIXTURES / "freehire.json").read_text())["data"]]
DJINNI = [DjinniAdapter.parse(i, remote=True) for i in parse_rss((FIXTURES / "djinni.xml").read_text())]
WWR = [WeWorkRemotelyAdapter.parse(i) for i in parse_rss((FIXTURES / "weworkremotely.xml").read_text())]


def test_canonical_url_drops_tracking_params_www_and_trailing_slash():
    assert canonical_url("https://djinni.co/jobs/851382-x/?utm_source=freehire.me") == "djinni.co/jobs/851382-x"
    assert (
        canonical_url("https://www.WeWorkRemotely.com/remote-jobs/a?utm_campaign=x&ref=y")
        == "weworkremotely.com/remote-jobs/a"
    )
    assert (
        canonical_url("https://boards.example.com/jobs?gh_jid=12&utm_medium=x") == "boards.example.com/jobs?gh_jid=12"
    )


def test_freehire_copy_of_a_djinni_job_merges_by_url_and_keeps_richest_data():
    fh = next(p for p in FREEHIRE if p.extra.get("origin") == "djinni")
    dj = next(p for p in DJINNI if p.external_id == "851382")
    merged = dedupe_across_sources([dj, fh])
    assert len(merged) == 1
    job = merged[0]
    assert job.company == fh.company and job.location == "Europe"  # Djinni's RSS has neither
    assert job.source_urls == {"freehire": fh.url, "djinni": dj.url}
    assert job.url == dj.url  # the board's own listing stays the link
    assert job.extra["merged_from"] == ["djinni", "freehire"]
    assert len(job.description) == max(len(fh.description), len(dj.description))


def test_freehire_title_with_company_prefix_merges_with_wwr_and_keeps_wwr_location():
    fh = next(p for p in FREEHIRE if ": " in p.title)  # "<company>: <title>" copied from WWR
    prefix, _, title = fh.title.partition(": ")
    wwr = next(p for p in WWR if p.company == prefix and p.title == title)
    merged = dedupe_across_sources([fh, wwr])
    assert len(merged) == 1
    job = merged[0]
    assert job.url == wwr.url and set(job.source_urls) == {"freehire", "weworkremotely"}
    # FreeHire's copy came through a third-party board that says "Denmark"; WWR listed it worldwide.
    assert job.location == "Anywhere in the World"


def test_whole_fixture_set_collapses_only_the_shared_jobs():
    everything = [*FREEHIRE, *DJINNI, *WWR]
    merged = dedupe_across_sources(everything)
    assert len(merged) == len(everything) - 2
    assert sum(1 for p in merged if len(p.source_urls) > 1) == 2
    assert all(p.source_urls for p in merged)


def test_same_title_twice_on_one_board_stays_two_openings():
    a = JobPosting(
        source="jobicy", external_id="1", url="https://jobicy.com/jobs/1", title="Account Executive", company="Acme"
    )
    b = replace(a, external_id="2", url="https://jobicy.com/jobs/2")
    assert len(dedupe_across_sources([a, b])) == 2
    other_board = replace(
        a, source="weworkremotely", external_id="w", url="https://weworkremotely.com/remote-jobs/acme-ae"
    )
    assert len(dedupe_across_sources([a, b, other_board])) == 2


def test_merge_fills_missing_facts_from_other_records():
    lean = JobPosting(
        source="djinni",
        external_id="1",
        url="https://djinni.co/jobs/1-x/",
        title="Python Developer",
        description="long " * 100,
    )
    rich = JobPosting(source="freehire", external_id="s", url="https://djinni.co/jobs/1-x/?utm_source=freehire.me",
                      title="Python Developer", company="Acme", applied_count=3, view_count=40,
                      employment_type="full_time", salary_max=4000, salary_currency="USD", salary_period="month",
                      tags=["python"], description="short")  # fmt: skip
    job = merge_postings([lean, rich])
    assert job.source == "freehire" and job.description == lean.description
    assert (job.applied_count, job.salary_max, job.salary_period) == (3, 4000, "month")
    assert job.url == lean.url


def test_single_posting_gets_its_own_source_url():
    p = WWR[0]
    assert merge_postings([p]).source_urls == {"weworkremotely": p.url}
