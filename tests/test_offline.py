"""Offline tests: fake models, the example person, a temporary workspace. No API keys, no network, no cost.

    pytest            (or: python tests/test_offline.py)
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from groundtruth import applications as A, cli, docs, factsheet as F, llm, match as M, posting as P, tailor as T, writer as W  # noqa: E402
from groundtruth.settings import Settings  # noqa: E402
from groundtruth.workspace import Workspace  # noqa: E402

EX = ROOT / "groundtruth" / "examples"
POSTING = (EX / "job_posting.example.txt").read_text()


def workspace() -> Workspace:
    d = Path(tempfile.mkdtemp())
    assert cli.main(["init", "--dir", str(d)]) == 0
    llm.configure(d, "balanced")
    return Workspace.find(str(d))


# ---------------------------------------------------------------- fact sheet
def test_fact_sheet():
    text = (EX / "fact_sheet.example.md").read_text()
    facts = F.facts_by_id(text)
    assert {"A1", "A3", "B1", "D1", "D2", "E3", "F4", "H1"} <= set(facts)
    assert F.validate(text) == []
    assert "duplicate IDs: D1" in F.validate(text + "\n- D1. Again\n")[0]
    h = F.header(facts, "DE", ("A4", ["DE"]))
    assert h["name"] == "Alex Morgan" and h["email"] == "alex.morgan@example.com" and "+31 6 0000 0000" in h["contact"]
    assert h["permit"] == "EU citizen, no work permit needed in the EU"
    assert F.header(facts, "NL", ("A4", ["DE"]))["permit"] is None
    assert F.check_numbers("Moved 30 reporting queries to Snowflake", ["D1"], facts) is None
    assert "'45'" in F.check_numbers("Moved 45 reporting queries", ["D1"], facts)
    assert F.check_numbers("x", ["Z9"], facts).startswith("cites IDs")
    assert F.earliest_start(1, date(2026, 1, 31)) == "28 February 2026"
    assert F.earliest_start(2, date(2026, 11, 15)) == "15 January 2027"
    assert "{EARLIEST_START}" not in F.with_dates(text)


# ---------------------------------------------------------------- hard rules
def test_eligibility():
    s = Settings(languages={"English": "C2", "German": "A2"}, work_rights=["NL", "DE"])
    ex = lambda **k: M.Extracted(**{"is_job_posting": True, "title": "x", "country_code": "DE", **k})  # noqa: E731
    assert M.eligibility(ex(), s)[0] == "Eligible"
    lab, why = M.eligibility(ex(languages_required=[M.LangReq(language="German", level="C1", required=True)]), s)
    assert lab == "Not eligible" and "German C1" in why and "A2" in why
    assert M.eligibility(ex(languages_required=[M.LangReq(language="German", level="C1", required=False)]), s)[0] == "Eligible"
    assert M.eligibility(ex(languages_required=[M.LangReq(language="French", required=True)]), s)[0] == "Not eligible"
    assert M.eligibility(ex(country_code="GB"), s)[0] == "Unclear"
    assert M.eligibility(ex(country_code="GB", visa_sponsorship="offered"), s)[0] == "Likely"
    assert M.eligibility(ex(country_code="GB", right_to_work_required=True), s)[0] == "Not eligible"
    assert M.eligibility(ex(country_code="GB"), s.model_copy(update={"needs_sponsorship_elsewhere": False}))[0] == "Eligible"


# ---------------------------------------------------------------- reading postings
def test_posting():
    page = ('<html><body><div id="app"></div><script type="application/ld+json">{"@context":"https://schema.org",'
            '"@type":"JobPosting","title":"Analytics Engineer","hiringOrganization":{"name":"Example Mobility"},'
            '"validThrough":"2026-11-30","description":"<p>' + "Own our dbt project on Snowflake. " * 20 + '</p>"}'
            '</script></body></html>')
    t = P.jobposting_text(page)
    assert t and "Example Mobility" in t and "2026-11-30" in t and "<p>" not in t
    d = Path(tempfile.mkdtemp())
    (d / "job.html").write_text(page)
    assert P.read(file=str(d / "job.html"))[0].startswith("Analytics Engineer")
    assert P.read(text=POSTING)[1] == "pasted text"
    for bad in (dict(text="too short"), dict(url="https://www.linkedin.com/jobs/view/123"), dict()):
        try:
            P.read(**bad)
            raise AssertionError(f"should refuse {bad}")
        except P.PostingError:
            pass


# ---------------------------------------------------------------- model gateway
def _resp(text, finish="stop", model="m"):
    return NS(model=model, usage=NS(prompt_tokens=10, completion_tokens=5),
              choices=[NS(finish_reason=finish, message=NS(content=text))])


def test_llm_plan():
    from pydantic import BaseModel

    class Out(BaseModel):
        keep: list[int]

    workspace()
    calls = []

    def run(script):
        llm.S.native_refused.clear()
        calls.clear()

        def fake(role, messages, temperature, max_tokens, response_format=None, fallback=True):
            calls.append(dict(native=response_format is not None, fallback=fallback, mt=max_tokens,
                              in_prompt="JSON schema" in messages[0]["content"]))
            r = script[len(calls) - 1]
            if isinstance(r, Exception):
                raise r
            return r
        llm._complete = fake

    class BadRequestError(Exception):
        pass

    bad = BadRequestError("schema not supported")
    llm.S.roles["judge"] = {"model": "openai/x"}
    run([bad, _resp('{"keep":[1]}')])                       # native refused -> schema in the prompt, same model
    assert llm.ask("judge", "x", schema=Out).keep == [1]
    assert calls[0]["native"] and calls[1]["in_prompt"] and not calls[1]["fallback"]
    run([_resp("", finish="length"), _resp('{"keep":[2]}')])  # out of room -> more room
    assert llm.ask("judge", "x", schema=Out).keep == [2] and calls[1]["mt"] == 2 * calls[0]["mt"]
    run([bad, bad, _resp('{"keep":[3]}')])                  # backup only in the last step
    assert llm.ask("judge", "x", schema=Out).keep == [3] and [c["fallback"] for c in calls] == [False, False, True]
    llm.S.roles["judge"] = {"model": "anthropic/x"}
    run([_resp('{"keep":[4]}')])                            # Anthropic: schema in the prompt from the start
    assert llm.ask("judge", "x", schema=Out).keep == [4] and calls[0]["in_prompt"]
    import os
    os.environ["GROUNDTRUTH_MONTHLY_BUDGET_USD"] = "0"
    try:
        llm.ask("judge", "x")
        raise AssertionError("budget cap ignored")
    except llm.BudgetExceeded:
        pass
    finally:
        os.environ.pop("GROUNDTRUTH_MONTHLY_BUDGET_USD")


# ---------------------------------------------------------------- the whole pipeline with fake models
def fake_models(drafts: list):
    L = W.Line

    def extract(posting):
        return M.Extracted(is_job_posting=True, title="Analytics Engineer (m/f/d)", company="Example Mobility GmbH",
                           location="Berlin, Germany", country_code="DE", work_mode="hybrid",
                           languages_required=[M.LangReq(language="English", level="C1", required=True),
                                               M.LangReq(language="German", required=False)],
                           visa_sponsorship="not_mentioned")

    def score(facts_text, ex, posting, label, reason, s):
        assert "Alex Morgan" in facts_text and label == "Eligible"
        return M.Fit(fit_score=78, fit_type="direct", matched=["dbt on Snowflake -> D1, E1"], gaps=["No e-mobility domain"],
                     bridge="Logistics fleet data is close (D1, D2).", why="Direct dbt and Snowflake evidence.")

    def draft(facts_text, job, posting, s, feedback=None, revision=None):
        drafts.append({"feedback": feedback, "revision": revision})
        assert "available from" in facts_text and "{EARLIEST_START}" not in facts_text
        exp = [L(text="Senior Data Analyst | Northwind Logistics BV, Rotterdam, Netherlands | 03/2022 – present",
                 style="entry", fact_ids=["D1"]),
               L(text="Moved 30 reporting queries from SQL Server to Snowflake with dbt models and tests", style="bullet",
                 fact_ids=["D1", "E1"]),
               L(text="Led the design of the new data warehouse", style="bullet", fact_ids=["D1"])]  # overstated
        if feedback is None:
            exp.append(L(text="Cut reporting time by 80%", style="bullet", fact_ids=["D2"]))         # invented number
        skills = [L(text="Data engineering: SQL, dbt, Snowflake, star schemas", style="text", fact_ids=["E1", "E4"])]
        certs = [L(text="dbt Analytics Engineering Certification, 2024", style="text", fact_ids=["F4"])] if feedback else []
        if revision:
            skills.append(L(text="Python: pandas, scikit-learn", style="text", fact_ids=["E2"]))
        letter = [W.LetterLine(text="The Analytics Engineer role appeals to me because your city managers act on the data every morning.",
                               style="text", fact_ids=[], framing=True, para=1),
                  W.LetterLine(text="At Northwind I moved 30 reporting queries to Snowflake with dbt models and tests.",
                               style="text", fact_ids=["D1"], para=2),
                  W.LetterLine(text="I would welcome the chance to discuss the role.", style="text", fact_ids=[],
                               framing=True, para=4)]
        return W.JobDraft(
            headline=L(text="Senior Data Analyst | dbt and Snowflake | Data modelling", style="text", fact_ids=["D1", "E1", "E4"]),
            cv=[W.Section(heading="Skills", lines=skills), W.Section(heading="Work experience", lines=exp),
                W.Section(heading="Certificates", lines=certs + [L(text="[TO FILL: Snowflake certification?]", style="text",
                                                                   fact_ids=[], placeholder=True)])],
            letter=letter, posting_keywords=["dbt", "Snowflake", "Power BI", "Kubernetes"], photo_expected=False,
            photo_reason="international tech company", contact_person="Ms Jana Example", reference="AE-2026-14",
            employer_address=["Example Mobility GmbH", "Berlin, Germany"],
            emphasis=["Led with the dbt migration"], placeholders=["Snowflake certification"],
            change_log=["Done: added Python"] if revision else [])

    def verify(facts_text, numbered, posting=""):
        assert "Analytics Engineer" in posting, "the verifier sees the posting"
        return {n: W.Verdict(n=n, verdict="overstated" if "Led the design" in l.text else
                             "placeholder_ok" if "[TO FILL" in l.text else "framing_ok" if l.framing else "supported",
                             problem="the sheet says 'contributed to the design'" if "Led the design" in l.text else None)
                for n, l in numbered}

    M.extract, M.score, W.draft, W.verify = extract, score, draft, verify


def test_pipeline():
    ws = workspace()
    drafts = []
    fake_models(drafts)
    folder = T.match_posting(ws, text=POSTING, log=lambda *_: None)
    m = json.loads((folder / "match.json").read_text())
    assert m["fit"]["fit_score"] == 78 and m["eligibility"] == "Eligible" and (folder / "posting.txt").exists()

    r = T.tailor(ws, folder, log=lambda *_: None)
    assert len(drafts) == 2, "one redraft after failures"
    fb = drafts[1]["feedback"]
    assert "Led the design" in fb and "'80'" in fb, "both the verifier and the number check send lines back"
    assert "F4" in fb, "a must-include fact missing from draft 1 is asked for"
    assert "Power BI" in fb and "Kubernetes" not in fb, "only keywords the fact sheet supports are pushed"

    from pypdf import PdfReader
    cv = "\n".join(pg.extract_text() for pg in PdfReader(str(r["paths"]["cv_pdf"])).pages)
    assert "Alex Morgan" in cv and "Moved 30 reporting queries" in cv
    assert "Led the design" not in cv, "failed lines are never saved"
    assert "KEY SKILLS" in cv and "PROFESSIONAL EXPERIENCE" in cv and cv.index("KEY SKILLS") < cv.index("PROFESSIONAL EXPERIENCE")
    assert "[TO FILL" in cv and "dbt Analytics Engineering Certification" in cv
    assert r["paths"]["cv_pdf"].name == "Alex_Morgan_CV_Example_Mobility_GmbH.pdf"
    letter = "\n".join(pg.extract_text() for pg in PdfReader(str(r["paths"]["letter_pdf"])).pages)
    assert letter.count("Example Mobility GmbH") == 1, "employer named once in the address block"
    assert "Dear Ms Example," in letter and "Reference AE-2026-14" in letter and "Kind regards" in letter
    checks = r["paths"]["checks"].read_text()
    assert "Removed during verification" in checks and "Led the design" in checks
    assert "Kubernetes — not found in your fact sheet" in checks, "keywords you lack are reported, never added"
    assert r["removed"] == 1 and r["ats_passed"] >= r["ats_total"] - 2

    r2 = T.tailor(ws, folder, change="Add a Python skills line", log=lambda *_: None)
    assert "PREVIOUS VERSION" in drafts[-1]["revision"] and "Moved 30 reporting queries" in drafts[-1]["revision"]
    assert any("Done: added Python" in n for n in r2["notes"])
    assert list((folder / "earlier").iterdir()), "the earlier version is kept"
    saved = json.loads((folder / "draft.json").read_text())
    assert saved["requests"] == ["Add a Python skills line"]


def test_same_job_twice():
    """A job checked twice by mistake is recognised, reported, and never overwritten."""
    ws = workspace()
    drafts = []
    fake_models(drafts)
    scored = []
    _score = M.score
    M.score = lambda *a, **k: scored.append(1) or _score(*a, **k)
    first = T.match_posting(ws, url="https://careers.example.com/jobs/ae-2026-14?utm_source=linkedin", text=POSTING, log=lambda *_: None)
    before = (first / "match.json").read_text()
    assert A.current(first)["status"] == "matched"
    A.set_status(first, "applied", "2026-10-22")
    extracted = []
    _extract = M.extract
    M.extract = lambda body: extracted.append(1) or _extract(body)

    for kw, how, model_calls in ((dict(url="https://careers.example.com/jobs/ae-2026-14/"), "the same link", 0),
                                 (dict(text=POSTING + "\nApply via our careers page."), "the same posting text", 0),
                                 (dict(text=POSTING.replace("eleven", "twelve") * 0 + "Analytics Engineer at Example Mobility. "
                                       + "Totally rewritten text with other words. " * 20), "the same employer and job title", 1)):
        n_ex, n_sc = len(extracted), len(scored)
        try:
            T.match_posting(ws, **kw, log=lambda *_: None)
            raise AssertionError(f"not recognised: {how}")
        except A.AlreadyMatched as e:
            assert e.info["how"] == how and e.info["status"] == "applied" and e.info["since"] == "2026-10-22", e.info
            msg = str(e)
            assert "you APPLIED (since 2026-10-22)" in msg and "Nothing was changed" in msg and "78/100" in msg
        assert len(extracted) - n_ex == model_calls and len(scored) == n_sc, "never re-scored; at most the cheap extraction"
    assert (first / "match.json").read_text() == before and len(list(ws.applications.iterdir())) == 1, "the old entry is untouched"

    again = T.match_posting(ws, text=POSTING, log=lambda *_: None, again=True)
    assert again != first and again.name.endswith("-2") and (first / "match.json").read_text() == before, "--again: a new folder"
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(["match", "--dir", str(ws.root), "--text-file", str(EX / "job_posting.example.txt")]) == 0
        assert cli.main(["status", again.name, "not applying", "--dir", str(ws.root)]) == 0
        assert cli.main(["list", "--dir", str(ws.root)]) == 0
    txt = out.getvalue()
    assert "Already checked" in txt and "--again" in txt and "2 application(s)" in txt and "not applying" in txt and "applied" in txt
    try:
        A.set_status(first, "hired")
        raise AssertionError("unknown status accepted")
    except ValueError:
        pass
    M.extract, M.score = _extract, _score


def test_rules_and_style():
    s = Settings(never_include=[{"fact": "C2", "describe": "volunteer work"}], avoid_wording=[r"\bninja\b"])
    L = W.Line
    assert "never shown" in T.rule_problem(L(text="Club database", style="text", fact_ids=["C2"]), True, s)
    assert T.rule_problem(L(text="Club database", style="text", fact_ids=["C2"]), False, s) is None, "letters may mention it"
    assert "ninja" in T.rule_problem(L(text="SQL ninja", style="text", fact_ids=["E1"]), True, s)
    assert T.tidy("Data Analyst — Northwind", "entry") == "Data Analyst | Northwind"
    assert T.tidy("I built it — and it works.", "text") == "I built it, and it works."
    d = W.JobDraft(headline=L(text="Results-driven analyst", style="text", fact_ids=["D1"]), cv=[], letter=[],
                   posting_keywords=[], photo_expected=False, photo_reason="", emphasis=[], placeholders=[])
    assert T.style_issues(d) == [("Results-driven analyst", "Results-driven")]
    assert T.salutation("Prof. Dr. Anna Muller") == "Dear Prof. Dr. Muller," and T.salutation("Jana Example") == "Dear Jana Example,"
    assert T.salutation("Frau Jana Example") == "Dear Ms Example,"
    assert "Alex Morgan" not in W.writer_system(Settings()), "no person is baked into the prompts"


def test_photo_rule():
    pol = docs.photo_policy
    assert pol("DE", "We look forward to your application.", True, True)[0]
    assert not pol("DE", "Please apply without a photo.", True, True)[0]
    assert not pol("GB", "", True, True)[0] and not pol("NL", "", True, True)[0]
    assert not pol("DE", "", True, False)[0]


def test_check_command():
    ws = workspace()
    assert cli.main(["check", "--dir", str(ws.root)]) == 1, "the example person must be replaced before real use"
    t = ws.fact_sheet.read_text().replace("Alex Morgan", "Sam Rivera")
    ws.fact_sheet.write_text(t)
    assert cli.main(["check", "--dir", str(ws.root)]) == 0
    assert ".env" in (ws.root / ".gitignore").read_text()
    shutil.rmtree(ws.root)


def test_budget_ledger_and_batches():
    """Budget before every attempt, unknown prices estimated (never $0), a busy ledger loses nothing, the fact check
    runs in batches, and the finished document is reviewed after failing lines are removed."""
    import os
    import sqlite3
    from pydantic import BaseModel

    class Out(BaseModel):
        keep: list[int]

    os.environ["GROUNDTRUTH_MONTHLY_BUDGET_USD"] = "10"
    workspace()
    llm.S.roles["judge"] = {"model": "openai/unknown-test-model"}
    calls = []

    def fake(role, messages, temperature, max_tokens, response_format=None, fallback=True):
        calls.append(max_tokens)
        return script[len(calls) - 1]
    llm._complete = fake
    script = [_resp("not json"), _resp('{"keep":[1]}')]
    assert llm.ask("judge", "x", schema=Out).keep == [1]
    with llm._db() as con:
        rows = con.execute("select cost_usd, cost_kind from calls").fetchall()
    assert rows and all(c > 0 and k == "estimated" for c, k in rows), rows
    # the cap is checked before the second attempt, not only once per answer
    os.environ["GROUNDTRUTH_RUN_BUDGET_USD"] = str(llm.S.run_cost + 0.0001)
    calls.clear()
    script = [_resp("not json"), _resp('{"keep":[2]}')]
    try:
        llm.ask("judge", "x", schema=Out)
        raise AssertionError("run cap ignored")
    except llm.BudgetExceeded as e:
        assert "GROUNDTRUTH_RUN_BUDGET_USD" in str(e) and len(calls) == 0
    os.environ["GROUNDTRUTH_RUN_BUDGET_USD"] = "2"
    real = llm._db
    llm._db = lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
    llm._log("t", "judge", "m", 1, 1, 0.5, 1, True)
    llm._db = real
    assert llm._pending().exists()
    llm._log("t", "judge", "m", 1, 1, 0.25, 1, True)
    assert not llm._pending().exists()

    seen = []

    def fake_ask(role, prompt, **k):
        ns = [int(x.split(".")[0]) for x in prompt.split("LINES (cited IDs in brackets)\n")[1].splitlines()]
        seen.append(ns)
        return fresh.Check(lines=[{"n": n, "verdict": "supported"} for n in ns])
    import importlib.util
    spec = importlib.util.find_spec("groundtruth.writer")     # a fresh copy: other tests replace W.verify with a fake
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    fresh.ask = fake_ask
    lines = [(n, fresh.Line(text=f"line {n}", style="bullet", fact_ids=["D1"])) for n in range(1, 24)]
    v = fresh.verify("D1. fact", lines)
    assert sorted(v) == list(range(1, 24)) and len(seen) == 3 and all(len(b) <= fresh.VERIFY_BATCH for b in seen)
    assert T.final_review([{"heading": "Education", "lines": [1]}], []) == [
        "The Professional Experience section is empty or missing after the fact check", "The cover letter is empty after the fact check"]
    assert T.final_review([{"heading": "Professional Experience", "lines": [1]}, {"heading": "Education", "lines": [1]}], [{"text": "x"}]) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name}: OK")
