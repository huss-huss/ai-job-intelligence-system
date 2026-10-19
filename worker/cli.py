"""FirstFetch CLI: run discovery without Postgres/Redis.

    python -m worker.cli discover --profile profile.yaml --sources sources.yaml --top 15
    python -m worker.cli discover --source remotive --search python --json results.json
    python -m worker.cli notion-setup
    python -m worker.cli repair-text --dry-run   # needs DATABASE_URL
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import yaml

from worker.ai import build_extractor
from worker.config import settings
from worker.notion import NotionSync
from worker.pipeline import DiscoveryReport, discover
from worker.profile import CandidateProfile, load_profile
from worker.sources import SOURCE_TYPES

DEFAULT_SOURCES = [
    {"type": "remotive", "search": "python"},
    {"type": "remoteok", "keywords": ["python", "backend", "ai"]},
    {"type": "himalayas", "limit": 20, "keywords": ["python", "backend", "engineer"]},
]


def _profile(path: str | None) -> CandidateProfile:
    if path:
        return load_profile(path)
    for candidate in ("profile.yaml", "profile.example.yaml"):
        if Path(candidate).exists():
            return load_profile(candidate)
    return CandidateProfile()


def _sources(args) -> list[dict]:
    if args.sources:
        return yaml.safe_load(Path(args.sources).read_text())["sources"]
    if args.source:
        config: dict = {"type": args.source}
        if args.search:
            config["search" if args.source == "remotive" else "keywords"] = (
                args.search if args.source == "remotive" else [args.search]
            )
        if args.board:
            config["board" if args.source == "greenhouse" else "company"] = args.board
        return [config]
    return DEFAULT_SOURCES


def _print_table(report: DiscoveryReport, top: int, show_ineligible: bool) -> None:
    rows = [j for j in report.jobs if show_ineligible or j.match.eligible is not False][:top]
    print(f"\n{'Score':>5}  {'Elig':4}  {'Title':48}  {'Company':22}  Source")
    print("-" * 100)
    for j in rows:
        elig = {True: "yes", False: "no", None: "?"}[j.match.eligible]
        company = (j.posting.company or "")[:22]
        print(f"{j.score:>5}  {elig:4}  {j.posting.title[:48]:48}  {company:22}  {j.posting.source}")
        print(f"{'':13}{' · '.join(j.match.reasons)[:120]}")
        print(f"{'':13}{j.posting.url}")
    hidden = sum(1 for j in report.jobs if j.match.eligible is False)
    print("-" * 100)
    print(
        f"{report.fetched} fetched · {len(report.jobs)} unique · {hidden} not open to your location"
        f" · {report.llm_calls} LLM calls ({report.input_tokens + report.output_tokens} tokens)"
    )
    for name, err in report.errors.items():
        print(f"! source {name} failed: {err[:150]}")


def cmd_discover(args) -> int:
    profile = _profile(args.profile)
    extractor = build_extractor(settings.openai_api_key, settings.openai_model, settings.llm_base_url)
    report = discover(_sources(args), profile, extractor)
    _print_table(report, args.top, args.show_ineligible)

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                [
                    {
                        "title": j.posting.title,
                        "company": j.posting.company,
                        "url": j.posting.url,
                        "source": j.posting.source,
                        "location": j.posting.location,
                        "posted_at": j.posting.posted_at.isoformat() if j.posting.posted_at else None,
                        **j.match.to_dict(),
                    }
                    for j in report.jobs
                ],
                indent=2,
            )
        )
        print(f"wrote {len(report.jobs)} jobs to {args.json}")

    if args.notion:
        if not (settings.notion_token and settings.notion_database_id):
            print("NOTION_TOKEN and NOTION_DATABASE_ID must be set", file=sys.stderr)
            return 2
        stats = NotionSync(settings.notion_token, settings.notion_database_id).sync(
            report.jobs, min_score=profile.notify_threshold
        )
        print(f"Notion: {stats}")
    return 0


def cmd_notion_setup(args) -> int:
    if not (settings.notion_token and settings.notion_database_id):
        print("NOTION_TOKEN and NOTION_DATABASE_ID must be set", file=sys.stderr)
        return 2
    added = NotionSync(settings.notion_token, settings.notion_database_id).setup_database()
    print(f"Added properties: {', '.join(added)}" if added else "Database already has all properties")
    return 0


def cmd_repair_text(args) -> int:
    from sqlalchemy import create_engine, make_url
    from sqlalchemy.orm import Session

    url = args.database_url or os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL (or --database-url) must be set", file=sys.stderr)
        return 2
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    u = make_url(url)
    if u.drivername.startswith("postgresql"):
        u = u.set(drivername="postgresql+psycopg2")  # same as the worker's sync engine

    from worker.repair import repair_job_text

    with Session(create_engine(u)) as session:
        stats = repair_job_text(session, dry_run=args.dry_run, batch_size=args.batch_size)
    for job_id, column, before, after in stats.samples:
        print(f"  job {job_id} {column}: {before!r}\n  {'':>{len(str(job_id)) + len(column) + 5}}-> {after!r}")
    fields = ", ".join(f"{name}={count}" for name, count in stats.fields.items())
    verb = "would repair" if args.dry_run else "repaired"
    print(f"{stats.scanned} jobs scanned, {verb} {stats.changed} ({fields})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="firstfetch", description=__doc__.split("\n")[0])
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="fetch, score, and rank jobs")
    d.add_argument("--profile", help="profile YAML (default: profile.yaml, then profile.example.yaml)")
    d.add_argument("--sources", help="YAML file with a top-level 'sources' list")
    d.add_argument("--source", choices=SOURCE_TYPES, help="single source type")
    d.add_argument("--search", help="keyword for --source")
    d.add_argument("--board", help="Greenhouse board token or Lever company slug for --source")
    d.add_argument("--top", type=int, default=15)
    d.add_argument("--show-ineligible", action="store_true", help="include jobs not open to your location")
    d.add_argument("--json", help="write all scored jobs to this file")
    d.add_argument("--notion", action="store_true", help="sync matches above notify_threshold to Notion")
    d.set_defaults(func=cmd_discover)

    n = sub.add_parser("notion-setup", help="add FirstFetch properties to your Notion database")
    n.set_defaults(func=cmd_notion_setup)

    r = sub.add_parser("repair-text", help="fix garbled (mis-decoded) text already stored in jobs_raw")
    r.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    r.add_argument("--database-url", help="defaults to $DATABASE_URL")
    r.add_argument("--batch-size", type=int, default=500)
    r.set_defaults(func=cmd_repair_text)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
