"""Reading a job posting: pasted text, a saved file (PDF, Word, HTML, text) or a link.

Links are fetched once, politely: robots.txt is respected and nothing behind a login is attempted. Many job boards
(LinkedIn among them) block automated reading; for those, save the page as PDF (browser: Print -> Save as PDF) or
paste the text. The posting is always treated as untrusted data.
"""
from __future__ import annotations

import html as H
import json
import re
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

MIN_TEXT = 300
MAX_TEXT = 20000
UA = "groundtruth-cv/0.1 (+https://github.com/; reads one job posting at the user's request)"


class PostingError(RuntimeError):
    pass


def strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|nav|footer|header|noscript|svg)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", s)
    s = H.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def jobposting_text(page: str) -> str | None:
    """Text of a schema.org JobPosting embedded in the page (many career sites render the job only in script)."""
    for m in re.finditer(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if isinstance(it, dict) and "JobPosting" in str(it.get("@type")):
                org = it.get("hiringOrganization") or {}
                loc = it.get("jobLocation") or {}
                loc = loc[0] if isinstance(loc, list) and loc else loc
                addr = (loc.get("address") or {}) if isinstance(loc, dict) else {}
                parts = [it.get("title"), org.get("name") if isinstance(org, dict) else org,
                         ", ".join(str(addr.get(k)) for k in ("addressLocality", "addressCountry") if addr.get(k)) or None,
                         f"Apply by: {it['validThrough']}" if it.get("validThrough") else None,
                         strip_html(str(it.get("description") or ""))]
                text = "\n".join(str(p) for p in parts if p)
                if len(text) >= MIN_TEXT:
                    return text
    return None


def _robots_ok(url: str, client) -> bool:
    u = urlparse(url)
    rp = RobotFileParser()
    try:
        r = client.get(f"{u.scheme}://{u.netloc}/robots.txt", timeout=10)
        if r.status_code >= 400:
            return True
        rp.parse(r.text.splitlines())
    except Exception:  # noqa: BLE001  (no robots.txt reachable: allowed)
        return True
    return rp.can_fetch(UA, url)


def from_url(url: str) -> tuple[str, str]:
    import httpx
    if re.search(r"linkedin\.com", url):
        raise PostingError("LinkedIn does not allow automated reading. Save the job page as PDF (Print -> Save as PDF) "
                           "and use --file, or paste the text with --text-file")
    with httpx.Client(headers={"User-Agent": UA, "Accept-Language": "en"}, follow_redirects=True, timeout=20) as c:
        if not _robots_ok(url, c):
            raise PostingError("the site's robots.txt does not allow reading this page automatically. "
                               "Save it as PDF or paste the text instead")
        try:
            r = c.get(url)
        except Exception as e:  # noqa: BLE001
            raise PostingError(f"the page did not load ({type(e).__name__})") from e
    if r.status_code >= 400:
        raise PostingError(f"the page returned HTTP {r.status_code} (login wall, removed, or blocked)")
    page = r.text
    text = jobposting_text(page)
    if text:
        return text[:MAX_TEXT], "the job data embedded in the page"
    text = strip_html(page)
    if len(text) < MIN_TEXT:
        raise PostingError("the page holds almost no text (it is built in the browser). Save it as PDF or paste the text")
    return text[:MAX_TEXT], "the job page"


def from_file(path: str | Path) -> tuple[str, str]:
    p = Path(path).expanduser()
    if not p.exists():
        raise PostingError(f"no file at {p}")
    ext = p.suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            t = "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages[:12])
            if len(t.strip()) < MIN_TEXT:
                raise PostingError("the PDF has no selectable text (it is a picture or scan). Save the job page with "
                                   "Print -> Save as PDF in your browser, or paste the text")
        elif ext == ".docx":
            from docx import Document
            t = "\n".join(par.text for par in Document(str(p)).paragraphs)
        elif ext in (".html", ".htm"):
            raw = p.read_text(errors="replace")
            t = jobposting_text(raw) or strip_html(raw)
        else:
            t = p.read_text(errors="replace")
    except PostingError:
        raise
    except Exception as e:  # noqa: BLE001
        raise PostingError(f"the file could not be read ({type(e).__name__})") from e
    t = re.sub(r"[ \t]+", " ", t).strip()
    if len(t) < MIN_TEXT:
        raise PostingError("the file holds too little text to be a job posting")
    return t[:MAX_TEXT], f"the attached {ext.lstrip('.').upper() or 'file'}"


def read(url: str | None = None, file: str | None = None, text: str | None = None) -> tuple[str, str]:
    """Returns (posting text, how it was read)."""
    if text and len(text.strip()) >= MIN_TEXT:
        return text.strip()[:MAX_TEXT], "pasted text"
    if text:
        raise PostingError("the pasted text is too short to be a whole job posting")
    if file:
        return from_file(file)
    if url:
        return from_url(url)
    raise PostingError("give a link (--url), a file (--file) or the pasted text (--text-file)")
