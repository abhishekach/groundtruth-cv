"""groundtruth: honest job matching and fact-checked CV tailoring.

  groundtruth init                         create fact_sheet.md, settings.yaml and .env in this folder
  groundtruth check                        check your fact sheet's format before spending anything
  groundtruth match --url <link>           score your honest chance for one job (or --file / --text-file)
  groundtruth tailor <folder>              CV + cover letter for a matched job (or --url / --file / --text-file)
  groundtruth revise <folder> "<change>"   apply your change request to the last version
  groundtruth costs                        this month's model spend

Options for every command: --dir <workspace> (default: this folder) and --models <profile> (default: balanced).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from groundtruth import __version__, factsheet as F, llm
from groundtruth.posting import PostingError
from groundtruth.workspace import Workspace

EXAMPLES = Path(__file__).parent / "examples"


def _ws(a) -> Workspace:
    ws = Workspace.find(a.dir)
    if a.cmd not in ("init", "check"):
        llm.configure(ws.root, a.models)
    return ws


def cmd_init(a) -> int:
    ws = Workspace.find(a.dir)
    ws.root.mkdir(parents=True, exist_ok=True)
    pairs = [("fact_sheet.example.md", ws.fact_sheet), ("settings.example.yaml", ws.settings_file),
             ("env.example", ws.root / ".env")]
    for src, dst in pairs:
        if dst.exists():
            print(f"  kept     {dst.name} (already there)")
        else:
            shutil.copy(EXAMPLES / src, dst)
            print(f"  created  {dst.name}")
    gi = ws.root / ".gitignore"
    if not gi.exists():
        gi.write_text(".env\nfact_sheet.md\nsettings.yaml\napplications/\n.groundtruth/\n")
        print("  created  .gitignore (keeps your personal files out of any git repository)")
    print("\nNext:\n  1. Put your API key(s) in .env\n  2. Replace the example person in fact_sheet.md with your own facts"
          "\n  3. Adjust settings.yaml\n  4. groundtruth check, then groundtruth match --url <job link>")
    return 0


def cmd_check(a) -> int:
    ws = Workspace.find(a.dir)
    text = ws.facts_text()
    problems = F.validate(text)
    facts = F.facts_by_id(text)
    s = ws.settings()
    for it in [*s.must_include, *s.never_include] + ([s.permit_line] if s.permit_line else []):
        if it.fact not in facts:
            problems.append(f"settings.yaml names {it.fact}, which is not on the fact sheet")
    if "Alex Morgan" in text:
        problems.append("the fact sheet still describes the example person (Alex Morgan): replace it with your own facts")
    print(f"{len(facts)} facts found in {ws.fact_sheet.name}.")
    for p in problems:
        print(f"  - {p}")
    print("No problems found." if not problems else f"{len(problems)} problem(s) to fix.")
    return 1 if problems else 0


def _posting_args(a) -> dict:
    text = Path(a.text_file).expanduser().read_text(errors="replace") if a.text_file else None
    return {"url": a.url, "file": a.file, "text": text}


def _print_match(folder: Path) -> None:
    from groundtruth.tailor import load_match
    m, _ = load_match(folder)
    f = m["fit"]
    print(f"\n{f['fit_score']}/100 ({f['fit_type']}) · {m['eligibility']} · {m['title']} – {m['company'] or '?'} "
          f"({m['country']})")
    print(f"Why: {f['why']}")
    if f.get("matched"):
        print("Matched:\n" + "\n".join(f"  + {x}" for x in f["matched"][:8]))
    if f.get("gaps"):
        print("Gaps:\n" + "\n".join(f"  - {x}" for x in f["gaps"][:8]))
    if f.get("bridge"):
        print(f"Bridge: {f['bridge']}")
    if f.get("red_flags"):
        print("Red flags:\n" + "\n".join(f"  ! {x}" for x in f["red_flags"]))
    print(f"Right to work: {m['eligibility_reason']}")
    print(f"\nSaved in {folder}\nTo write the CV and cover letter: groundtruth tailor \"{folder}\"")


def cmd_match(a) -> int:
    ws = _ws(a)
    folder = match_or_fail(ws, a)
    if folder is None:
        return 1
    _print_match(folder)
    print(f"Model cost of this run: ${llm.S.run_cost:.3f}")
    return 0


def match_or_fail(ws, a) -> Path | None:
    from groundtruth.tailor import match_posting
    try:
        return match_posting(ws, **_posting_args(a))
    except PostingError as e:
        print(f"Could not read the job: {e}.")
        return None


def cmd_tailor(a, change: str | None = None) -> int:
    from groundtruth.tailor import tailor
    ws = _ws(a)
    if a.folder:
        folder = Path(a.folder).expanduser().resolve()
    else:
        folder = match_or_fail(ws, a)
        if folder is None:
            return 1
        _print_match(folder)
        print()
    r = tailor(ws, folder, change=change)
    p = r["paths"]
    print(f"\nDone: {r['verified']} lines verified, {r['removed']} removed, {r['placeholders']} to fill, "
          f"ATS checks {r['ats_passed']}/{r['ats_total']}"
          + (f", posting keywords in CV {r['keywords_pct']}%" if r["keywords_pct"] is not None else ""))
    for k in ("cv_pdf", "letter_pdf", "cv_docx", "letter_docx", "checks"):
        print(f"  {p[k]}")
    if r["placeholders"]:
        print("Open Checks.html: highlighted [TO FILL] items need your input before you send anything.")
    print(f"Model cost of this run: ${llm.S.run_cost:.3f}")
    return 0


def cmd_revise(a) -> int:
    change = a.change
    if change and Path(change).expanduser().is_file():
        change = Path(change).expanduser().read_text(encoding="utf-8")
    if not (change or "").strip():
        print("Say what to change, e.g. groundtruth revise <folder> \"Shorter profile; lead with the SQL work\"")
        return 2
    a.url = a.file = a.text_file = None
    return cmd_tailor(a, change=change.strip()[:3000])


def cmd_costs(a) -> int:
    _ws(a)
    rows = llm.month_summary()
    print(f"This month: ${llm.month_spend():.2f}")
    for task, role, model, n, cost in rows:
        print(f"  {task:<8} {role:<9} {model:<40} {n:>4} calls  ${cost or 0:.3f}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="groundtruth", description="Honest job matching and fact-checked CV tailoring.")
    ap.add_argument("--version", action="version", version=f"groundtruth-cv {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", help="workspace folder (default: this folder, or $GROUNDTRUTH_HOME)")
    common.add_argument("--models", help="model profile from models.yaml (default: balanced, or $GROUNDTRUTH_MODELS)")
    src = argparse.ArgumentParser(add_help=False)
    src.add_argument("--url", help="link to the job posting")
    src.add_argument("--file", help="the posting saved as PDF, Word, HTML or text")
    src.add_argument("--text-file", help="a text file holding the pasted posting")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", parents=[common], help="create your workspace files")
    sub.add_parser("check", parents=[common], help="check your fact sheet and settings")
    sub.add_parser("match", parents=[common, src], help="score one job against your fact sheet")
    t = sub.add_parser("tailor", parents=[common, src], help="write a fact-checked CV and cover letter")
    t.add_argument("folder", nargs="?", help="an application folder from `match`")
    r = sub.add_parser("revise", parents=[common], help="apply a change request to the last version")
    r.add_argument("folder")
    r.add_argument("change", help="what to change, in your words (or a file holding it)")
    sub.add_parser("costs", parents=[common], help="this month's model spend")
    a = ap.parse_args(argv)
    if a.cmd in ("match",) and not (a.url or a.file or a.text_file):
        ap.error("match needs --url, --file or --text-file")
    if a.cmd == "tailor" and not (a.folder or a.url or a.file or a.text_file):
        ap.error("tailor needs an application folder, or --url, --file or --text-file")
    handlers = {"init": cmd_init, "check": cmd_check, "match": cmd_match, "tailor": cmd_tailor,
                "revise": cmd_revise, "costs": cmd_costs}
    try:
        return handlers[a.cmd](a)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
