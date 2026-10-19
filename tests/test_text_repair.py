import json
import sqlite3
from pathlib import Path

import pytest

from worker.cli import main
from worker.sources.adapters import RemoteOKAdapter
from worker.text import fix_mojibake, html_to_text

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "mojibake.json").read_text())
GARBLED = ("Â", "Ã", "â€", "â\u0080")


def _clean(text: str) -> bool:
    return not any(m in text for m in GARBLED) and not any(0x80 <= ord(c) < 0xA0 for c in text)


def test_stored_description_from_live_dashboard_is_repaired():
    stored = FIXTURE["stored"]["description"]
    assert "Weâ\x80\x99re building" in stored  # the exact stored bytes (Latin-1 decoded UTF-8)
    fixed = fix_mojibake(stored)
    assert "We’re building" in fixed and "What You’ll Do" in fixed
    assert _clean(fixed)
    assert fix_mojibake(fixed) == fixed  # idempotent


@pytest.mark.parametrize("sample", FIXTURE["upstream"], ids=lambda s: s["company"])
def test_upstream_remoteok_html_is_repaired(sample):
    text = html_to_text(sample["description"])
    assert text and _clean(text)


def test_upstream_symbols_decode_to_intended_characters():
    by_company = {s["company"]: html_to_text(s["description"]) for s in FIXTURE["upstream"]}
    assert "™" in by_company["Allego"] and "®" in by_company["Allego"]  # ™ ®
    assert "–" in by_company["Packmatic GmbH"]  # –


def test_remoteok_adapter_repairs_title_company_and_description():
    posting = RemoteOKAdapter.parse({
        "id": "1", "url": "https://remoteok.com/x", "position": "CafÃ© Engineer â\u0080\u0094 AI",
        "company": "WrapbookÂ®", "description": "<p>Weâ\u0080\u0099re hiring</p>",
    })  # fmt: skip
    assert posting.title == "Café Engineer — AI"
    assert posting.company == "Wrapbook®"
    assert posting.description == "We’re hiring"


@pytest.mark.parametrize(
    ("garbled", "expected"),
    [
        ("Weâ€ re building", "We’re building"),  # third byte became a space (issue #8)
        ("Weâ€re building", "We’re building"),  # third byte dropped
        ("youâ€ ll get", "you’ll get"),
        ("donâ€t", "don’t"),
        ("the â€œbestâ€ and more", "the “best” and more"),  # 0x9D dropped
        ("the â€œbestâ€", "the “best”"),
        ("â€œquotedâ€.", "“quoted”."),
        ("Weâ\u0080 re", "We’re"),  # Latin-1 flavour of the same truncation
        ("Weâ€™re and 日本", "We’re and 日本"),  # mixed with real CJK
        ("launch ðŸš€ now", "launch \U0001f680 now"),  # 4-byte emoji
        ("Ã©tÃ©", "été"),
        ("ÃƒÂ©", "é"),  # mis-decoded twice
    ],
)
def test_fix_mojibake_repairs(garbled, expected):
    assert fix_mojibake(garbled) == expected
    assert fix_mojibake(expected) == expected


@pytest.mark.parametrize(
    "legit",
    [
        "pâte, château, hâte-toi",  # French
        "não, SÃO PAULO, João, Ângela, lâmpada",  # Portuguese
        "Â â Ã",  # lone letters
        "a â€ b",  # truncated but ambiguous: leave it alone
        "price â € 5",
        "café » naïve © 2026 °C",
        "Øl ° ß ½",
        "We’re “fine” — really \U0001f680",
        "",
        "plain text",
    ],
)
def test_fix_mojibake_leaves_legitimate_text_alone(legit):
    assert fix_mojibake(legit) == legit


def _sqlite_db(path: Path) -> str:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE jobs_raw (id INTEGER PRIMARY KEY, title TEXT, company TEXT, description TEXT, meta TEXT)")
    con.executemany(
        "INSERT INTO jobs_raw VALUES (?, ?, ?, ?, ?)",
        [
            (1, "Senior Engineer", "Wrapbook", FIXTURE["stored"]["description"], '{"k": 1}'),
            (2, "CafÃ© Lead", "AcmeÂ®", None, None),
            (3, "Engenheiro de Dados", "SÃO PAULO Ltda", "não â pâte", None),
        ],
    )
    con.commit()
    con.close()
    return f"sqlite:///{path}"


def _rows(path: Path) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT id, title, company, description, meta FROM jobs_raw ORDER BY id").fetchall()
    finally:
        con.close()


def test_repair_text_cli_dry_run_then_apply_is_idempotent(tmp_path, capsys):
    db = tmp_path / "jobs.db"
    url = _sqlite_db(db)
    before = _rows(db)

    assert main(["repair-text", "--dry-run", "--database-url", url, "--batch-size", "2"]) == 0
    out = capsys.readouterr().out
    assert "3 jobs scanned, would repair 2 (title=1, company=1, description=1)" in out
    assert _rows(db) == before  # dry run writes nothing

    assert main(["repair-text", "--database-url", url, "--batch-size", "2"]) == 0
    assert "repaired 2" in capsys.readouterr().out
    rows = _rows(db)
    assert "We’re building" in rows[0][3] and _clean(rows[0][3]) and rows[0][4] == '{"k": 1}'
    assert rows[1][1:3] == ("Café Lead", "Acme®")
    assert rows[2] == before[2]  # legitimate Portuguese/French text untouched

    assert main(["repair-text", "--database-url", url]) == 0
    assert "repaired 0" in capsys.readouterr().out
    assert _rows(db) == rows


def test_repair_text_requires_database_url(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["repair-text", "--dry-run"]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err
