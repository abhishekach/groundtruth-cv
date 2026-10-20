"""The fact sheet: the ONLY source of facts about you. Every CV line must cite the IDs it rests on.

Format (Markdown; see examples/fact_sheet.example.md):

    ## A. Identity and contact
    - A1. Name: Alex Morgan
    - A2. Location: Rotterdam, Netherlands
    - A3. Email: alex@example.com · Phone: +31 6 0000 0000 · LinkedIn: linkedin.com/in/example
    ## D. Professional experience
    - D1. Example BV, Rotterdam: Data Analyst, 03/2021 - present. Built the weekly sales dashboard ...

IDs are a capital letter plus a number. Text in [square brackets] is a note to the writer (a source, "do not use",
"skill claim only"); it never backs a claim on its own.
"""
from __future__ import annotations

import calendar
import re
from collections import Counter
from datetime import date

START_TOKEN = "{EARLIEST_START}"
ID = r"[A-Z]\d{1,3}"
FACT_RE = re.compile(rf"^\s*-\s*({ID})\.\s*(.+?)(?=^\s*-\s*{ID}\.|^#|\Z)", re.S | re.M)
ID_LINE_RE = re.compile(rf"^\s*-\s*({ID})\.", re.M)
NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
NOTE_RE = re.compile(r"\[[^\]]*\]|\([^()]*(?:confirm|question|never|do not|verify|source)[^()]*\)", re.I)


def facts_by_id(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in FACT_RE.finditer(text)}


def earliest_start(months: int = 1, today: date | None = None) -> str:
    """Today plus N calendar months (31 Jan + 1 -> 28/29 Feb), e.g. '19 November 2026'."""
    d = today or date.today()
    m0 = d.month - 1 + months
    y, m = d.year + m0 // 12, m0 % 12 + 1
    s = date(y, m, min(d.day, calendar.monthrange(y, m)[1]))
    return f"{s.day} {s.strftime('%B')} {s.year}"


def with_dates(text: str, months: int = 1, today: date | None = None) -> str:
    """The fact sheet with {EARLIEST_START} filled in for an application made today."""
    return text.replace(START_TOKEN, earliest_start(months, today))


def claims(text: str) -> str:
    """Fact text without notes, so a note's date ("confirmed 14 Oct") never backs a number."""
    return NOTE_RE.sub(" ", text)


def check_numbers(text: str, fact_ids: list[str], facts: dict[str, str]) -> str | None:
    """Code check for one line: cited IDs exist and every number in the line appears in the cited facts."""
    if not fact_ids:
        return "no fact IDs cited"
    missing = [i for i in fact_ids if i not in facts]
    if missing:
        return f"cites IDs not on the fact sheet: {', '.join(missing)}"
    cited = claims(" ".join(facts[i] for i in fact_ids))
    norm = lambda s: s.replace(",", ".")  # noqa: E731
    for num in NUM_RE.findall(text):
        if norm(num) not in norm(cited) and num not in cited:
            return f"number '{num}' does not appear in the cited facts"
    return None


def _strip(s: str) -> str:
    return re.sub(r"\s*\[[^\]]*\]", "", s or "").strip()


def header(facts: dict[str, str], country: str | None = None, permit: tuple[str, list[str]] | None = None) -> dict:
    """Name and contact block from A1-A3 (plain text at the top of the body, where parsers look), plus an optional
    permit line for jobs in the given countries."""
    name = _strip(re.sub(r"^Name:\s*", "", facts.get("A1", ""))) or "[TO FILL: name in A1]"
    loc = _strip(re.sub(r"^Location:\s*", "", facts.get("A2", "")))
    a3 = _strip(facts.get("A3", ""))
    email = (re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", a3) or [None])[0]
    phone = (re.search(r"\+?\d[\d ()-]{7,}\d", a3) or [None])[0]
    links = re.findall(r"(?:https?://)?(?:www\.)?(?:linkedin\.com|github\.com)/[^\s·|,;]+", a3)
    site = re.search(r"(?i)website:\s*(\S+)", a3)
    links += [site.group(1)] if site else []
    contact = " | ".join(x for x in [loc, email, phone, *links] if x)
    permit_text = None
    if permit and (country or "").upper() in {c.upper() for c in permit[1]}:
        raw = facts.get(permit[0], "")
        m = re.search(r'CV line:\s*"([^"]+)"', raw)
        permit_text = m.group(1) if m else _strip(raw)
    return {"name": name, "location": loc, "email": email, "phone": phone, "links": links,
            "contact": contact, "permit": permit_text}


def validate(text: str) -> list[str]:
    """Problems that would make the writer or the checks misbehave."""
    out = []
    ids = ID_LINE_RE.findall(text)
    if not ids:
        return ["no facts found: each fact is a list item starting with an ID, e.g. '- D1. ...'"]
    dup = [i for i, n in Counter(ids).items() if n > 1]
    if dup:
        out.append(f"duplicate IDs: {', '.join(sorted(dup))} (each ID must be unique, or citations become ambiguous)")
    facts = facts_by_id(text)
    for k, what in (("A1", "your name"), ("A3", "your email and phone")):
        if k not in facts:
            out.append(f"{k} is missing: it holds {what}, which the CV header needs")
    if "A3" in facts and not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", facts["A3"]):
        out.append("A3 has no email address")
    if not any(k.startswith("D") for k in facts):
        out.append("no D facts (professional experience): the CV will have no experience section")
    return out
