"""One-off maintenance: repair mis-decoded text already stored in jobs_raw.

Re-running analysis never rewrites jobs_raw text, so rows ingested before a text fix keep their
garbled titles/descriptions. `repair_job_text` applies `fix_mojibake` to the stored columns and
writes back only rows that change, which makes it safe to run repeatedly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, select, update
from sqlalchemy.orm import Session

from worker.text import fix_mojibake

TEXT_COLUMNS = ("title", "company", "description")

# Only the columns this command touches, so it works on any database that has jobs_raw
# without importing the full backend models.
_jobs_raw = Table(
    "jobs_raw",
    MetaData(),
    Column("id", Integer, primary_key=True),
    Column("title", String(512)),
    Column("company", String(512)),
    Column("description", Text),
)


@dataclass
class RepairStats:
    scanned: int = 0
    changed: int = 0
    fields: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TEXT_COLUMNS, 0))
    samples: list[tuple[int, str, str, str]] = field(default_factory=list)  # (id, column, before, after)


def _snippet(before: str, after: str, width: int = 60) -> tuple[str, str]:
    start = next((i for i, (a, b) in enumerate(zip(before, after, strict=False)) if a != b), 0)
    start = max(0, start - 15)
    return before[start : start + width], after[start : start + width]


def repair_job_text(
    session: Session, *, dry_run: bool = True, batch_size: int = 500, max_samples: int = 10
) -> RepairStats:
    """Fix mojibake in jobs_raw.title/company/description. Commits per batch unless dry_run."""
    stats = RepairStats()
    cols = [_jobs_raw.c[name] for name in TEXT_COLUMNS]
    last_id = 0
    while True:
        rows = session.execute(
            select(_jobs_raw.c.id, *cols).where(_jobs_raw.c.id > last_id).order_by(_jobs_raw.c.id).limit(batch_size)
        ).all()
        if not rows:
            break
        for row in rows:
            stats.scanned += 1
            changes = {}
            for name in TEXT_COLUMNS:
                value = getattr(row, name)
                if value and (fixed := fix_mojibake(value)) != value:
                    changes[name] = fixed
                    stats.fields[name] += 1
                    if len(stats.samples) < max_samples:
                        stats.samples.append((row.id, name, *_snippet(value, fixed)))
            if changes:
                stats.changed += 1
                if not dry_run:
                    session.execute(update(_jobs_raw).where(_jobs_raw.c.id == row.id).values(**changes))
        last_id = rows[-1].id
        if not dry_run:
            session.commit()
    return stats
