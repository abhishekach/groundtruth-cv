"""Your applications: what you did with each job, and recognising a job you have already checked.

A job you check again (the same link, the same posting text, or the same employer and job title) is never re-scored
or overwritten: groundtruth tells you when you checked it, its fit score, whether a CV was written and what you did
with it. `--again` checks it again into a NEW folder; the old one is always kept.

Status per application, with the day it changed (status.json in its folder):
    matched -> tailored -> applied -> interview -> offer | rejected | withdrawn | not applying
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from pathlib import Path

STATUSES = ["matched", "tailored", "applied", "interview", "offer", "rejected", "withdrawn", "not applying"]


class AlreadyMatched(RuntimeError):
    """The job is already in your applications; `info` says what you did with it. Nothing was changed."""

    def __init__(self, info: dict):
        self.info = info
        super().__init__(already_text(info))


# ---------------------------------------------------------------- reading the applications folder
def _read(folder: Path) -> dict | None:
    m = folder / "match.json"
    if not m.exists():
        return None
    try:
        d = json.loads(m.read_text(encoding="utf-8"))
    except ValueError:
        return None
    d["folder"] = folder
    p = folder / "posting.txt"
    d["posting"] = p.read_text(encoding="utf-8") if p.exists() else ""
    return d


def history(folder: Path) -> list[dict]:
    f = folder / "status.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return []


def current(folder: Path) -> dict:
    """The latest status with its day; without a record: tailored if a CV exists, else matched."""
    h = history(folder)
    if h:
        return h[-1]
    tailored = next(iter(sorted(folder.glob("*_CV_*.pdf"))), None)
    if tailored:
        return {"status": "tailored", "date": date.fromtimestamp(tailored.stat().st_mtime).isoformat()}
    d = _read(folder) or {}
    return {"status": "matched", "date": (d.get("checked") or "")[:10] or None}


def set_status(folder: Path, status: str, day: str | None = None, note: str = "") -> dict:
    status = status.strip().lower().replace("_", " ")
    if status not in STATUSES:
        raise ValueError(f"unknown status '{status}'. Use one of: {', '.join(STATUSES)}")
    if not (folder / "match.json").exists():
        raise FileNotFoundError(f"{folder} is not an application folder")
    h = history(folder)
    entry = {"status": status, "date": day or date.today().isoformat(), **({"note": note} if note else {})}
    h.append(entry)
    (folder / "status.json").write_text(json.dumps(h, indent=1, ensure_ascii=False), encoding="utf-8")
    return entry


def all_applications(root: Path) -> list[dict]:
    out = []
    for folder in sorted(root.glob("*/")) if root.exists() else []:
        d = _read(folder)
        if not d:
            continue
        cur = current(folder)
        cv = next(iter(sorted(folder.glob("*_CV_*.pdf"))), None)
        out.append({"folder": folder, "checked": (d.get("checked") or "")[:10], "title": d.get("title"), "company": d.get("company"),
                    "country": d.get("country"), "url": d.get("url"), "score": (d.get("fit") or {}).get("fit_score"),
                    "eligibility": d.get("eligibility"), "status": cur["status"], "since": cur.get("date"),
                    "cv": date.fromtimestamp(cv.stat().st_mtime).isoformat() if cv else None, "posting": d["posting"]})
    return out


# ---------------------------------------------------------------- recognising the same job
def norm_url(u: str | None) -> str:
    u = (u or "").strip().lower()
    m = re.search(r"linkedin\.com/.*?(?:jobs/view/(?:[^/?]*-)?|currentjobid=)(\d{6,})", u)
    if m:
        return f"linkedin:{m.group(1)}"
    u = re.sub(r"^https?://(www\.)?", "", u)
    u = re.sub(r"#.*$", "", u)
    u = re.sub(r"[?&](utm_[^=&]+|ref|refid|trk|source|src|gclid|fbclid)=[^&]*", "", u)
    return u.rstrip("/?&")


def norm_name(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = re.sub(r"\(?\b(m|w|d|f|x)\s*/\s*(m|w|d|f|x)(\s*/\s*(m|w|d|f|x))?\b\)?", " ", s)      # (m/w/d)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b(gmbh|ag|ltd|limited|inc|llc|bv|b v|plc|se|sa|srl|co|kg)\b", " ", s)
    return " ".join(s.split())


def _words(t: str | None) -> set[str]:
    return set(re.findall(r"[a-zäöüß0-9]{3,}", (t or "").lower()))


def same_text(a: str, b: str | None) -> bool:
    """Two postings with at least 90% of their distinct words in common (reposts, small edits, extra tracking lines)."""
    x, y = _words(a[:8000]), _words((b or "")[:8000])
    return len(x) > 40 and len(y) > 40 and len(x & y) / len(x | y) >= 0.9


def find(root: Path, url: str | None = None, body: str | None = None, title: str | None = None,
         company: str | None = None) -> dict | None:
    """The earliest application for this job, with `how` it was recognised, or None."""
    apps = sorted(all_applications(root), key=lambda a: a["checked"])
    nu = norm_url(url) if url else ""
    for a in apps:
        if nu and a["url"] and norm_url(a["url"]) == nu:
            return {**a, "how": "the same link"}
    if body:
        for a in apps:
            if same_text(body, a["posting"]):
                return {**a, "how": "the same posting text"}
    if title:
        nt, nc = norm_name(title), norm_name(company)
        for a in apps:
            if norm_name(a["title"]) == nt and norm_name(a["company"]) == nc:
                return {**a, "how": "the same employer and job title"}
    return None


STATUS_TEXT = {"matched": "checked, no CV written yet", "tailored": "CV and cover letter written, not marked as applied",
               "applied": "you APPLIED", "interview": "you have an INTERVIEW", "offer": "you have an OFFER",
               "rejected": "you were rejected", "withdrawn": "you withdrew", "not applying": "you decided not to apply"}


def already_text(a: dict) -> str:
    st = STATUS_TEXT.get(a["status"], a["status"]) + (f" (since {a['since']})" if a.get("since") and a["status"] != "matched" else "")
    score = f"{a['score']}/100" if a.get("score") is not None else "not scored"
    return (f"Already checked: {a['title']} – {a['company'] or '?'} (recognised by {a['how']}). First checked {a['checked']}, "
            f"{score}; {st}. Nothing was changed. Folder: {a['folder']}")
