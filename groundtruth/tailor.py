"""The pipeline: match a posting, then draft, check, redraft once, drop what still fails, and write the files.

applications/<date>-<employer>-<id>/
    posting.txt                         the posting as read
    match.json                          extracted facts, right-to-work label, fit score
    <Name>_CV_<Employer>.pdf / .docx    ATS-safe: one column, plain text, standard headings, MM/YYYY dates
    <Name>_Cover_Letter_<Employer>.pdf / .docx
    Checks.html                         ATS read-back, posting keywords, photo decision, line-by-line fact check
    draft.json                          the kept lines with their fact IDs (used by `revise`)
    earlier/<time>/                     the previous files, whenever a rewrite would overwrite them
Never submits anything and never contacts anyone.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path

from groundtruth import docs, factsheet as F, match as M, posting as P, writer as W
from groundtruth.settings import Settings
from groundtruth.workspace import Workspace

BAD = ("unsupported", "overstated")
ALIASES = {
    "summary": "Profile", "professional summary": "Profile", "professional profile": "Profile", "profile": "Profile",
    "skills": "Key Skills", "core skills": "Key Skills", "key skills": "Key Skills", "core competencies": "Key Skills",
    "experience": "Professional Experience", "work experience": "Professional Experience",
    "professional experience": "Professional Experience", "employment history": "Professional Experience",
    "projects": "Selected Projects", "selected projects": "Selected Projects", "education": "Education",
    "languages": "Languages & Certificates", "languages and certificates": "Languages & Certificates",
    "languages & certificates": "Languages & Certificates", "certificates": "Languages & Certificates",
    "interests": "Interests",
}
CLICHES = ["delve", "leverag", "spearhead", "synergy", "synergies", "seamless", "robust", "cutting-edge", "cutting edge",
           "state-of-the-art", "game-changer", "game changer", "passionate", "thrilled", "excited to", "eager to",
           "dynamic", "fast-paced", "results-driven", "detail-oriented", "proven track record", "track record of",
           "testament", "tapestry", "landscape", "realm", "navigate the complex", "navigating the complex", "in today's",
           "ever-evolving", "unwavering", "foster", "holistic", "pivotal", "meticulous", "showcase", "embark",
           "journey", "unlock", "elevate", "empower", "utilize", "utilise", "furthermore", "moreover", "additionally",
           "in conclusion", "i am confident that", "great fit", "look no further", "express my interest",
           "i am writing to"]
CLICHE_RE = re.compile(r"(?i)(" + "|".join(re.escape(c) for c in CLICHES) + r")")


def canon(heading: str) -> str:
    return ALIASES.get(re.sub(r"\s+", " ", heading.strip().lower()), heading.strip())


def slug(s: str | None, n: int = 45) -> str:
    s = re.sub(r"\s*\(.*?\)", "", s or "")
    s = re.sub(r"[^\w]+", "_", s, flags=re.UNICODE).strip("_")
    return s[:n].strip("_") or "Employer"


def tidy(text: str, style: str) -> str:
    """Machine typography out: em dashes become ' | ' in entry lines and commas in prose; stray spaces go."""
    t = text.replace("—", " | " if style == "entry" else ", ")
    t = re.sub(r"\s+,", ",", t)
    t = re.sub(r",\s*,", ",", t)
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"\s+([.;:])", r"\1", t)
    return t.strip()


# ---------------------------------------------------------------- matching
def match_posting(ws: Workspace, url=None, file=None, text=None, log=print) -> Path:
    """Reads and scores one posting; returns its application folder."""
    s = ws.settings()
    facts_text = ws.facts_text()
    body, how = P.read(url=url, file=file, text=text)
    log(f"Read the posting from {how}. Extracting its facts ...")
    ex = M.extract(body)
    if not ex.is_job_posting:
        raise P.PostingError("that text is not a single job posting (it looks like a list, login or error page). "
                             "Paste the job text instead")
    label, reason = M.eligibility(ex, s)
    log(f"Right to work: {label} ({reason}). Scoring your fit ...")
    fit = M.score(facts_text, ex, body, label, reason, s)
    cc = (ex.country_code or "XX").upper()
    key = hashlib.sha1(f"{(ex.company or '').lower()}|{ex.title.lower()}|{cc}".encode()).hexdigest()[:6]
    folder = ws.applications / f"{date.today().isoformat()}-{slug(ex.company, 30)}-{key}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "posting.txt").write_text(body, encoding="utf-8")
    (folder / "match.json").write_text(json.dumps({
        "url": url, "read_from": how, "title": ex.title, "company": ex.company, "location": ex.location, "country": cc,
        "eligibility": label, "eligibility_reason": reason, "extracted": ex.model_dump(), "fit": fit.model_dump(),
        "checked": datetime.now().isoformat(timespec="seconds")}, indent=1, ensure_ascii=False), encoding="utf-8")
    return folder


def load_match(folder: Path) -> tuple[dict, str]:
    m = folder / "match.json"
    if not m.exists():
        raise FileNotFoundError(f"{folder} has no match.json. Run `groundtruth match` first.")
    return json.loads(m.read_text(encoding="utf-8")), (folder / "posting.txt").read_text(encoding="utf-8")


# ---------------------------------------------------------------- checks on a draft
def _lines(d: W.JobDraft) -> list:
    return [d.headline] + [l for s in d.cv for l in s.lines] + list(d.letter)


def _cv_text(d: W.JobDraft) -> str:
    return " ".join([d.headline.text] + [l.text for s in d.cv for l in s.lines])


def code_check(line: W.Line, facts: dict) -> str | None:
    if line.framing:
        return "a framing sentence may not contain a number" if F.NUM_RE.search(line.text) else None
    if line.placeholder or "[TO FILL" in line.text:
        return None if "[TO FILL" in line.text else "marked placeholder but has no [TO FILL: ...]"
    return F.check_numbers(line.text, line.fact_ids, facts)


def rule_problem(line: W.Line, on_cv: bool, s: Settings) -> str | None:
    """The candidate's own rules from settings.yaml, enforced by code whatever the models do."""
    for pat in s.avoid_wording:
        if re.search(pat, line.text, re.I):
            return f"uses wording your settings rule out ('{pat}')"
    if on_cv:
        for it in s.never_include:
            if it.fact in line.fact_ids:
                return f"{it.fact} is never shown on a job CV" + (f" ({it.describe})" if it.describe else "")
    return None


def check_all(facts_text: str, facts: dict, d: W.JobDraft, s: Settings, posting: str = "") -> tuple[list, dict[int, str]]:
    """Numbered lines and {n: problem} for every failing line: code checks first, then the verifier model."""
    numbered, problems = [], {}
    n_cv = 1 + sum(len(x.lines) for x in d.cv)
    for n, l in enumerate(_lines(d), 1):
        numbered.append((n, l))
        p = code_check(l, facts) or rule_problem(l, n <= n_cv, s)
        if p:
            problems[n] = f"code check: {p}"
    verdicts = W.verify(facts_text, numbered, posting)
    for n, l in numbered:
        v = verdicts.get(n)
        if v is None:
            problems.setdefault(n, "verifier returned no verdict")
        elif v.verdict in BAD:
            problems.setdefault(n, f"{v.verdict}: {v.problem or ''}".strip())
        elif v.verdict == "placeholder_ok" and "[TO FILL" not in l.text:
            problems.setdefault(n, "verifier called it a placeholder but it has no [TO FILL]")
        elif v.verdict == "framing_ok" and not l.framing:
            problems.setdefault(n, "a CV or evidence line with no fact behind it")
    return numbered, problems


def keyword_gaps(d: W.JobDraft, facts_text: str) -> list[str]:
    """Posting keywords the fact sheet supports but the CV does not use yet."""
    cov = docs.keyword_coverage(d.posting_keywords or [], _cv_text(d), facts_text)
    return [c["term"] for c in cov if not c["covered"] and c["on_sheet"]]


def missing_required(d: W.JobDraft, s: Settings) -> list[str]:
    cited = {i for sec in d.cv for l in sec.lines for i in l.fact_ids}
    return [it.fact for it in s.must_include if it.fact not in cited]


def style_issues(d: W.JobDraft) -> list[tuple[str, str]]:
    out = []
    for l in _lines(d):
        m = CLICHE_RE.search(l.text)
        if m:
            out.append((l.text, m.group(1)))
        elif "—" in l.text or "!" in l.text:
            out.append((l.text, "em dash" if "—" in l.text else "exclamation mark"))
    return out


def two_passes(facts_text: str, facts: dict, make, s: Settings, revising: bool = False, log=print, posting: str = ""):
    """Draft and check; one redraft with precise feedback if anything failed; check again."""
    log("Revising (writer model) ..." if revising else "Drafting (writer model) ...")
    d = make(None)
    numbered, problems = check_all(facts_text, facts, d, s, posting)
    gaps = [] if revising else keyword_gaps(d, facts_text)
    req = [] if revising else missing_required(d, s)
    style = style_issues(d)
    log(f"  draft 1: {len(numbered)} lines, {len(problems)} failed the fact check, {len(gaps)} supported keyword(s) "
        f"missing" + (f", must-include missing: {', '.join(req)}" if req else ""))
    if problems or gaps or req or style:
        fb = []
        if style:
            fb.append("These lines sound machine-written. Rewrite each in plain, specific language without the flagged "
                      "word:\n" + "\n".join(f"- \"{t}\" -> avoid '{p}'" for t, p in style))
        if req:
            need = {it.fact: it.describe for it in s.must_include}
            fb.append("The CV must include:\n" + "\n".join(f"- {k}: {need[k]}".rstrip(": ") for k in req))
        if problems:
            fb.append("These lines failed the fact check. Fix or drop each one:\n" +
                      "\n".join(f"- \"{l.text}\" -> {problems[n]}" for n, l in numbered if n in problems))
        if gaps:
            fb.append("These posting keywords are supported by the fact sheet but missing from the CV. Work each one in, "
                      "in the posting's exact wording, only where the cited facts truly support it:\n"
                      + "\n".join(f"- {g}" for g in gaps))
        log("Redrafting with the checks' feedback ...")
        d = make("\n\n".join(fb))
        numbered, problems = check_all(facts_text, facts, d, s, posting)
        log(f"  draft 2: {len(numbered)} lines, {len(problems)} still failing (removed)")
    return d, problems


def keep(lines: list, start: int, problems: dict, rows: list, removed: list) -> list[dict]:
    out = []
    for i, l in enumerate(lines):
        n = start + i
        if n in problems:
            removed.append({"text": l.text, "why": problems[n]})
            continue
        row = l.model_dump()
        row["text"] = tidy(row["text"], row["style"])
        out.append(row)
        rows.append({"text": row["text"], "ids": ["framing"] if l.framing else l.fact_ids,
                     "result": "placeholder" if "[TO FILL" in l.text else "OK"})
    return out


def sections(secs, start: int, problems, rows, removed) -> tuple[list[dict], int]:
    out, n = [], start
    for sec in secs:
        lines = keep(sec.lines, n, problems, rows, removed)
        n += len(sec.lines)
        h = canon(sec.heading)
        prev = next((x for x in out if x["heading"] == h), None)
        if prev:
            prev["lines"] += lines
        else:
            out.append({"heading": h, "lines": lines})
    order = {h: i for i, h in enumerate(W.HEADINGS)}
    out.sort(key=lambda x: order.get(x["heading"], len(order)))
    return [x for x in out if x["lines"]], n


# ---------------------------------------------------------------- the letter frame
TITLES = {"ms", "mr", "mrs", "mx", "dr", "prof", "professor", "frau", "herr"}
TITLE_EN = {"frau": "Ms", "herr": "Mr"}


def salutation(contact: str | None) -> str | None:
    """'Ms Jana Example' -> 'Dear Ms Example,'; 'Prof. Dr. Anna Muller' -> 'Dear Prof. Dr. Muller,'; a name without a
    title stays whole ('Dear Jana Example,'), because guessing someone's title is worse than using the full name."""
    if not contact:
        return None
    toks = re.sub(r"\s+", " ", contact.strip()).split(" ")
    titles = []
    while toks and toks[0].lower().rstrip(".") in TITLES:
        t = toks.pop(0)
        titles.append(TITLE_EN.get(t.lower().rstrip("."), t))
    if not toks:
        return None
    if any(t.lower().rstrip(".") in ("dr", "prof", "professor") for t in titles):
        titles = [t for t in titles if t.lower().rstrip(".") not in ("ms", "mr", "mrs", "mx")]
    return f"Dear {' '.join(titles + [toks[-1]])}," if titles else f"Dear {' '.join(toks)},"


def paragraphs(letter: list[dict]) -> list[str]:
    out: dict[int, list[str]] = {}
    for l in letter:
        out.setdefault(int(l.get("para") or 1), []).append(l["text"].strip())
    return [" ".join(v) for _, v in sorted(out.items())]


def _dedupe(lines: list) -> list[str]:
    out, seen = [], set()
    for x in lines:
        k = re.sub(r"\W+", " ", (x or "").lower()).strip()
        if k and k not in seen:
            seen.add(k)
            out.append(x.strip())
    return out


def letter_frame(head: dict, recipient: str, contact: str | None, address: list[str], subject: str,
                 paras: list[str]) -> dict:
    city = (head.get("location") or "").split(",")[0].strip()
    today = date.today().strftime("%d %B %Y").lstrip("0")
    return {"sender": [x for x in [head["name"], head.get("location"),
                                   " | ".join(x for x in [head.get("email"), head.get("phone")] if x),
                                   *(head.get("links") or [])] if x],
            "recipient": _dedupe([recipient, contact, *address]),
            "place_date": f"{city}, {today}" if city else today,
            "subject": subject, "salutation": salutation(contact) or "Dear Hiring Team,",
            "paragraphs": paras, "closing": "Kind regards,"}


# ---------------------------------------------------------------- files
def keep_earlier(folder: Path) -> None:
    files = [f for f in folder.glob("*") if f.is_file() and f.name not in ("posting.txt", "match.json")]
    if not any(f.suffix in (".pdf", ".docx") for f in files):
        return
    dst = folder / "earlier" / datetime.now().strftime("%Y%m%d-%H%M%S")
    dst.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, dst / f.name)


def previous_version(folder: Path) -> dict | None:
    f = folder / "draft.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def tailor(ws: Workspace, folder: Path, change: str | None = None, log=print) -> dict:
    """Writes (or revises) the CV and cover letter for one matched posting. Returns a summary."""
    s = ws.settings()
    m, posting = load_match(folder)
    facts_text = F.with_dates(ws.facts_text(), s.start_after_months)
    facts = F.facts_by_id(facts_text)
    revision, requests = None, []
    if change:
        prev = previous_version(folder)
        if not prev:
            raise RuntimeError("there is no earlier version to revise here: run `groundtruth tailor` first")
        revision = W.revision_block(W.previous_text(prev), change)
        requests = [*(prev.get("requests") or []), change.strip()]
    job = {"title": m["title"], "company": m["company"], "location": m["location"], "country": m["country"], "fit": m["fit"]}
    d, problems = two_passes(facts_text, facts,
                             lambda fb: W.draft(facts_text, job, posting, s, feedback=fb, revision=revision),
                             s, revising=bool(change), log=log, posting=posting)
    rows, removed = [], []
    headline = keep([d.headline], 1, problems, rows, removed)
    cv, n = sections(d.cv, 2, problems, rows, removed)
    letter = keep(d.letter, n, problems, rows, removed)

    cc = (m.get("country") or "").upper()
    head = F.header(facts, cc, (s.permit_line.fact, s.permit_line.countries) if s.permit_line else None)
    head["headline"] = headline[0]["text"] if headline else None
    photo = ws.photo()
    use, photo_note = docs.photo_policy(cc, posting, d.photo_expected, photo is not None)
    if d.photo_reason:
        photo_note += f" (writer's view: {d.photo_reason.strip()})"
    head["photo"] = photo if use else None
    employer = slug(m.get("company"))
    ref = f" – Reference {d.reference}" if d.reference else ""
    L = letter_frame(head, m.get("company") or "", d.contact_person, d.employer_address,
                     f"Application for the position of {m['title']}{ref}", paragraphs(letter))
    notes = []
    if change:
        notes += [f"Your change request: {change.strip()}"] + (d.change_log or ["(no change log returned; check the documents)"])
    notes += [*d.emphasis, *[f"TO FILL: {x}" for x in d.placeholders]]
    if not change:
        notes += [f"Not on this CV although settings require it (left out twice): {k}" for k in missing_required(d, s)]
    notes += [f"Wording to check (sounds machine-written: '{p}'): {t[:120]}" for t, p in style_issues(d)]

    keep_earlier(folder)
    if head.get("photo"):
        if docs.photo_low_res(head["photo"]):
            photo_note += " The photo file is small and may print soft; 600 x 750 px or more is better."
        head["photo"] = docs.prepared_photo(head["photo"], folder)
    who = slug(head["name"], 40)
    paths = {"cv_docx": folder / f"{who}_CV_{employer}.docx", "cv_pdf": folder / f"{who}_CV_{employer}.pdf",
             "letter_docx": folder / f"{who}_Cover_Letter_{employer}.docx",
             "letter_pdf": folder / f"{who}_Cover_Letter_{employer}.pdf", "checks": folder / "Checks.html"}
    place_date = L["place_date"] if cc in ("DE", "AT", "CH") else None   # customary on CVs in German-speaking markets
    docs.cv_docx(head, cv, paths["cv_docx"], place_date)
    docs.cv_pdf(head, cv, paths["cv_pdf"], place_date)
    docs.letter_docx(head, L, paths["letter_docx"])
    docs.letter_pdf(head, L, paths["letter_pdf"])
    ats = docs.ats_check(paths["cv_docx"], paths["cv_pdf"], head, [x["heading"] for x in cv], d.posting_keywords,
                         max_pages=s.cv_max_pages, facts_text=facts_text)
    lc = docs.letter_check(paths["letter_docx"], paths["letter_pdf"], tuple(s.letter_words), 1, "Cover Letter")
    ats["checks"] += lc
    ats["passed"] += sum(c["ok"] for c in lc)
    ats["total"] += len(lc)
    title = f"{m.get('company') or 'Employer'} – {m['title'][:70]} – {date.today().isoformat()}"
    paths["checks"].write_text(docs.checks_html(docs.CSS, title, ats, rows, removed, notes, photo_note), encoding="utf-8")
    (folder / "draft.json").write_text(json.dumps({"headline": headline[0] if headline else None, "cv": cv, "letter": letter,
                                                   "requests": requests}, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"folder": folder, "paths": paths, "verified": len(rows), "removed": len(removed),
            "placeholders": len(d.placeholders), "ats_passed": ats["passed"], "ats_total": ats["total"],
            "keywords_pct": ats["pct"], "notes": notes}
