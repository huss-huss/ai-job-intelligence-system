"""Profile editing from Telegram: /profile, /skills, /titles, /salary, /country, /seniority, /employment.

Every change is parsed strictly here, validated again by CandidateProfile (worker/profile.py), stored
on the user, and then the user's recent jobs are rescored with the new profile, so /top and the next
digest reflect it immediately. Edits only ever touch the sender's own profile.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta

from pydantic import ValidationError

from app import users
from app.telegram.render import esc

RESCORE_DAYS = 30  # same window as /top
MAX_SKILLS = 40
MAX_TITLES = 30
MAX_SALARY = 1_000_000
SENIORITY = ("intern", "junior", "mid", "senior", "lead")
EMPLOYMENT = ("full_time", "part_time", "contract", "internship")
_EMPLOYMENT_ALIASES = {
    "fulltime": "full_time",
    "full": "full_time",
    "permanent": "full_time",
    "parttime": "part_time",
    "part": "part_time",
    "freelance": "contract",
    "contractor": "contract",
    "intern": "internship",
}
SKILL_RE = re.compile(r"[a-z0-9][a-z0-9+#./ -]{0,39}")
TITLE_RE = re.compile(r"[a-z0-9][a-z0-9+#./ &-]{1,59}")
COUNTRY_RE = re.compile(r"[A-Za-z][A-Za-z .'-]{1,55}")
GENERIC_TERMS = ("worldwide", "anywhere", "global")

_AFRICA = """algeria angola benin botswana burkina faso burundi cameroon cape verde chad comoros congo djibouti
egypt eritrea eswatini ethiopia gabon gambia ghana guinea kenya lesotho liberia libya madagascar malawi mali
mauritania mauritius morocco mozambique namibia niger nigeria rwanda senegal seychelles sierra leone somalia
south africa south sudan sudan tanzania togo tunisia uganda zambia zimbabwe"""
_MENA = "bahrain iran iraq israel jordan kuwait lebanon oman palestine qatar saudi arabia syria turkey uae yemen"
_EUROPE = """albania austria belgium bosnia bulgaria croatia cyprus czechia czech republic denmark estonia finland
france germany greece hungary iceland ireland italy kosovo latvia lithuania luxembourg malta moldova montenegro
netherlands north macedonia norway poland portugal romania serbia slovakia slovenia spain sweden switzerland
ukraine united kingdom uk"""


def regions_for(country: str) -> list[str]:
    c = country.lower()
    for block, terms in ((_AFRICA, ["africa", "emea"]), (_MENA, ["mena", "emea"]), (_EUROPE, ["europe", "emea"])):
        if re.search(rf"(?<![a-z]){re.escape(c)}(?![a-z])", " ".join(block.split())):
            return terms
    return []


class Reject(ValueError):
    """Bad input; the message is shown to the user."""


# --------------------------------------------------------------------------- parsing


def _skill(raw: str) -> str:
    skill = re.sub(r"\s+", " ", raw.strip().lower())
    if not SKILL_RE.fullmatch(skill):
        raise Reject("A skill is 1-40 characters: letters, digits, spaces and + # . / -")
    return skill


def _title(raw: str) -> str:
    title = re.sub(r"\s+", " ", raw.strip().lower())
    if not TITLE_RE.fullmatch(title):
        raise Reject("A title is 2-60 characters: letters, digits, spaces and + # . / & -")
    return title


def parse_salary(raw: str) -> float | None:
    text = raw.strip().lower().replace(",", "").replace("$", "").replace("usd", "").strip()
    if text in ("none", "off", "0", "-"):
        return None
    m = re.fullmatch(r"(\d{1,7}(?:\.\d+)?)\s*(k)?", text)
    if not m:
        raise Reject("Usage: /salary &lt;minimum yearly USD&gt;, e.g. /salary 30000 or /salary 30k (none to clear)")
    value = float(m.group(1)) * (1000 if m.group(2) else 1)
    if not 0 < value <= MAX_SALARY:
        raise Reject(f"The minimum salary must be between 1 and {MAX_SALARY:,} USD a year")
    return value


def parse_levels(args: list[str]) -> list[str]:
    levels = [w for w in re.split(r"[,\s]+", " ".join(args).lower()) if w]
    bad = [w for w in levels if w not in SENIORITY]
    if not levels or bad:
        raise Reject(f"Usage: /seniority &lt;levels&gt;, any of {', '.join(SENIORITY)}, e.g. /seniority mid,senior")
    return list(dict.fromkeys(levels))


def parse_employment(args: list[str], current: dict[str, float]) -> dict[str, float]:
    usage = (
        f"Usage: /employment &lt;types&gt;, any of {', '.join(EMPLOYMENT)}, optionally with a weight 0-1, "
        "e.g. /employment full_time,contract:0.6"
    )
    out: dict[str, float] = {}
    for part in [w for w in re.split(r"[,\s]+", " ".join(args).lower()) if w]:
        name, _, weight = part.partition(":")
        name = name.replace("-", "_")
        name = _EMPLOYMENT_ALIASES.get(name.replace("_", ""), _EMPLOYMENT_ALIASES.get(name, name))
        if name not in EMPLOYMENT:
            raise Reject(usage)
        if weight:
            try:
                value = float(weight)
            except ValueError:
                raise Reject(usage) from None
            if not 0 <= value <= 1:
                raise Reject(usage)
        else:
            value = current.get(name, 1.0)
        out[name] = value
    if not out:
        raise Reject(usage)
    return out


# --------------------------------------------------------------------------- edits


def edit_skills(data: dict, args: list[str]) -> str:
    usage = "Usage: /skills add &lt;skill&gt; [weight 1-3] · /skills remove &lt;skill&gt;"
    if len(args) < 2 or args[0].lower() not in ("add", "remove"):
        raise Reject(usage)
    action, rest = args[0].lower(), args[1:]
    skills: dict[str, int] = dict(data.get("skills") or {})
    if action == "add":
        weight = 2
        if len(rest) > 1 and rest[-1].isdigit():
            weight = int(rest[-1])
            rest = rest[:-1]
        if weight not in (1, 2, 3):
            raise Reject("The weight is 1 (nice to have), 2 or 3 (core)")
        skill = _skill(" ".join(rest))
        if skill not in skills and len(skills) >= MAX_SKILLS:
            raise Reject(f"At most {MAX_SKILLS} skills; remove one first")
        skills[skill] = weight
        message = f"Skill <b>{esc(skill)}</b> set to weight {weight}."
    else:
        skill = _skill(" ".join(rest))
        if skill not in skills:
            raise Reject(f"<b>{esc(skill)}</b> isn't one of your skills. /profile lists them.")
        del skills[skill]
        message = f"Skill <b>{esc(skill)}</b> removed."
    data["skills"] = skills
    return message


def edit_titles(data: dict, args: list[str]) -> str:
    usage = "Usage: /titles add &lt;title&gt; · /titles remove &lt;title&gt;, e.g. /titles add platform engineer"
    if len(args) < 2 or args[0].lower() not in ("add", "remove"):
        raise Reject(usage)
    title = _title(" ".join(args[1:]))
    titles: list[str] = list(data.get("target_titles") or [])
    if args[0].lower() == "add":
        if title in titles:
            return f"<b>{esc(title)}</b> is already a target title."
        if len(titles) >= MAX_TITLES:
            raise Reject(f"At most {MAX_TITLES} target titles; remove one first")
        titles.append(title)
        message = f"Target title <b>{esc(title)}</b> added."
    else:
        if title not in titles:
            raise Reject(f"<b>{esc(title)}</b> isn't one of your target titles. /profile lists them.")
        titles.remove(title)
        if not titles:
            raise Reject("Keep at least one target title")
        message = f"Target title <b>{esc(title)}</b> removed."
    data["target_titles"] = titles
    return message


def edit_salary(data: dict, args: list[str]) -> str:
    if not args:
        raise Reject("Usage: /salary &lt;minimum yearly USD&gt;, e.g. /salary 30000 (none to clear)")
    value = parse_salary(" ".join(args))
    data["min_salary_usd"] = value
    return "Minimum salary cleared." if value is None else f"Minimum salary set to ${value:,.0f} a year."


def edit_country(data: dict, args: list[str]) -> str:
    usage = "Usage: /country &lt;name&gt; [ISO code], e.g. /country Kenya ke"
    if not args:
        raise Reject(usage)
    code = ""
    if len(args) > 1 and re.fullmatch(r"[A-Za-z]{2}", args[-1]):
        code, args = args[-1].lower(), args[:-1]
    name = re.sub(r"\s+", " ", " ".join(args)).strip()
    if not COUNTRY_RE.fullmatch(name):
        raise Reject(usage)
    name = " ".join(w if w.isupper() and len(w) <= 3 else w.capitalize() for w in name.split())
    location = dict(data.get("location") or {})
    old = str(location.get("country") or "").lower()
    stale = {old, *regions_for(old)} if old else set()
    keep = [t for t in location.get("eligible_terms") or [] if t not in stale and t not in GENERIC_TERMS]
    terms = list(dict.fromkeys([*GENERIC_TERMS, name.lower(), *regions_for(name), *keep]))
    location.update(country=name, country_code=code, eligible_terms=terms)
    data["location"] = location
    return f"Country set to <b>{esc(name)}</b>. Jobs open to: {esc(', '.join(terms))}."


def edit_seniority(data: dict, args: list[str]) -> str:
    data["seniority"] = parse_levels(args)
    return f"Seniority set to {esc(', '.join(data['seniority']))}."


def edit_employment(data: dict, args: list[str]) -> str:
    data["employment_types"] = parse_employment(args, dict(data.get("employment_types") or {}))
    shown = ", ".join(f"{k} {v:g}" for k, v in data["employment_types"].items())
    return f"Employment types set to {esc(shown)}."


EDITS: dict[str, Callable[[dict, list[str]], str]] = {
    "skills": edit_skills,
    "titles": edit_titles,
    "salary": edit_salary,
    "country": edit_country,
    "seniority": edit_seniority,
    "employment": edit_employment,
}


def apply_edit(session, user, name: str, args: list[str], now) -> str:
    """Run one edit command for `user`. Returns the reply (HTML). Never raises for bad input."""
    users._worker_on_path()
    from worker.user_scoring import rescore_user

    data = users.profile_model(user).model_dump(mode="json")
    try:
        message = EDITS[name](data, args)
        profile = users.save_profile(session, user, data, now)
    except Reject as exc:
        return str(exc)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        return f"That doesn't fit the profile: {esc(where)}: {esc(first.get('msg'), 200)}"
    n = rescore_user(session, user, since=now - timedelta(days=RESCORE_DAYS), profile=profile, now=now)
    session.commit()
    return f"{message}\nRescored your {n} job{'s' if n != 1 else ''} from the last {RESCORE_DAYS} days."


def summary(user) -> str:
    p = users.profile_model(user)
    skills = ", ".join(f"{s} {w}" for s, w in sorted(p.skills.items(), key=lambda kv: (-kv[1], kv[0]))) or "none"
    employment = ", ".join(f"{k} {v:g}" for k, v in p.employment_types.items()) or "any"
    salary = f"${p.min_salary_usd:,.0f}/yr" if p.min_salary_usd else "not set"
    lines = [
        f"<b>Your profile</b> ({esc(p.name, 80)})",
        f"Titles: {esc(', '.join(p.target_titles), 600) or 'none'}",
        f"Skills (weight): {esc(skills, 1200)}",
        f"Seniority: {esc(', '.join(p.seniority)) or 'any'}",
        f"Country: {esc(p.location.country)} · open to: {esc(', '.join(p.location.eligible_terms), 300)}",
        f"Minimum salary: {salary}",
        f"Employment: {esc(employment)}",
        f"Timezone: {esc(p.timezone)}",
        "",
        "Change it with /skills, /titles, /salary, /country, /seniority, /employment.",
    ]
    return "\n".join(lines)
