# Groundtruth CV

**Honest job matching and fact-checked CV tailoring. It never invents experience.**

Most AI CV tools make you look good by making things up: a "led" where you "contributed", a tool you never used,
a number nobody measured. Groundtruth CV works the other way round. You write down what is true about you once, in a
fact sheet. For each job, it tells you your honest chance of being shortlisted, then writes a tailored CV and cover
letter in which **every line cites the facts it rests on** and is checked by code and by a second, independent model.
Lines that cannot be backed are removed, never saved.

```
groundtruth match --url https://careers.example.com/jobs/analytics-engineer

78/100 (direct) · Eligible · Analytics Engineer (m/f/d) – Example Mobility GmbH (DE)
Why: Direct dbt and Snowflake evidence; the e-mobility domain is new.
Matched:
  + dbt on Snowflake -> D1, E1
  + Dimensional modelling -> E4
Gaps:
  - No e-mobility domain experience
Bridge: Fleet and depot data from logistics is close (D1, D2).

groundtruth tailor applications/2026-10-20-Example_Mobility_GmbH-3fa9c1

Drafting (writer model) ...
  draft 1: 31 lines, 2 failed the fact check, 1 supported keyword(s) missing
Redrafting with the checks' feedback ...
  draft 2: 30 lines, 0 still failing (removed)
Done: 30 lines verified, 0 removed, 1 to fill, ATS checks 18/18, posting keywords in CV 85%
```

## What it does

1. **Reads the posting** from a link, a saved PDF or Word file, or pasted text. Links are read once, politely:
   robots.txt is respected, and sites that block automated reading (LinkedIn, for example) are never scraped. Save
   those as PDF instead.
2. **Applies your hard rules in code**, not by asking a model: languages the job requires at a level you don't have,
   and countries where you would need a work permit the employer doesn't offer.
3. **Scores your honest chance** of being shortlisted (0-100), with the requirements you meet (citing your facts),
   the gaps, and one honest way to bridge the biggest gap.
4. **Writes the CV and cover letter** as Word and PDF files built to pass applicant tracking systems: one column,
   no tables or text boxes, standard headings, MM/YYYY dates, contact details as text, selectable PDF text.
5. **Checks every line** of both documents:
   - **code** checks that every cited fact ID exists and that every number in a line appears in the facts it cites;
   - **a verifier model** (by default from a different vendor than the writer) marks each line supported,
     overstated or unsupported;
   - failing lines get one redraft with precise feedback, and are **removed** if they still fail.
6. **Reads the files back** the way an applicant tracking system does and writes `Checks.html`: what passed, which
   of the posting's keywords your CV covers, which ones your fact sheet does not support (and so were left out), the
   photo decision, and the line-by-line fact check.
7. **Revises on request**: `groundtruth revise <folder> "Shorter profile; lead with the SQL training"` applies your
   change and keeps everything else, still under the same rules. Earlier versions are kept.

It never applies to anything, sends anything or contacts anyone. It drafts; you decide.

## Quick start

Requires Python 3.11+ and an API key for at least one model provider.

```bash
pip install git+https://github.com/<owner>/groundtruth-cv.git

mkdir my-applications && cd my-applications
groundtruth init      # creates fact_sheet.md, settings.yaml, .env and a .gitignore that keeps them private
```

1. Put your API key(s) in `.env`.
2. Replace the example person in `fact_sheet.md` with your own facts (see below).
3. Adjust `settings.yaml`: what you are going for, your languages, where you can work.
4. Check the format, then try a job:

```bash
groundtruth check
groundtruth match --url <job link>            # or --file job.pdf, or --text-file job.txt
groundtruth tailor <the folder it printed>    # or tailor --url ... to match and write in one go
groundtruth costs                             # this month's model spend
```

Every application gets its own folder under `applications/`, with the posting, the match, both documents in Word
and PDF, `Checks.html` and the version history.

## The fact sheet

`fact_sheet.md` is the only source of facts about you. One fact per list item, each with a unique ID:

```markdown
## D. Professional experience
- D1. Northwind Logistics BV, Rotterdam, Netherlands: Senior Data Analyst, 03/2022 – present.
  Owns the weekly delivery-performance dashboard used by 12 depot managers. Moved 30 reporting queries from a
  legacy SQL Server to Snowflake with dbt models and tests. Contributed to the design of the new data warehouse.

## E. Skills
- E3. BI tools: Power BI (daily); Tableau [skill claim only: one course, no work use].
```

- **Write facts at their true strength.** "Contributed to" will never become "led" on your CV.
- **Numbers must be on the sheet.** A line saying "12 depot managers" may cite D1; "15 depot managers" fails.
- **[Notes in brackets]** guide the writer ("do not use", "skill claim only", a source) and never back a claim.
- `A1` (name), `A2` (location) and `A3` (email, phone, links) are required; they fill the header.
- `{EARLIEST_START}` becomes today's date plus `start_after_months` from your settings.

The more complete and precise your fact sheet, the better your CVs. Gaps show up as yellow `[TO FILL: ...]`
placeholders, never as guesses.

See [`groundtruth/examples/fact_sheet.example.md`](groundtruth/examples/fact_sheet.example.md) for a full example
(a made-up person).

## Settings

`settings.yaml` holds your rules, so nothing personal lives in the code:

| Setting | What it does |
|---|---|
| `target` | One paragraph on who you are and what you are going for. Both the judge and the writer read it. |
| `playbooks` | How the CV leads for each kind of role, citing fact IDs. The writer picks the best fit. |
| `must_include` / `never_include` | Facts that must appear on every job CV, or never. Enforced by code. |
| `avoid_wording` | Phrases your CV must never use, on top of the built-in list of machine-sounding words. |
| `languages` | Your CEFR levels. A posting that *requires* more is marked Not eligible. |
| `work_rights` | Countries where you can already work. Elsewhere, a job counts only with sponsorship. |
| `hard_rules` | Extra rules for the fit judge, in plain words. |
| `permit_line`, `photo` | Optional work-permit line and photo, used only where they are customary. |

See [`groundtruth/examples/settings.example.yaml`](groundtruth/examples/settings.example.yaml).

## Models and cost

Code asks for a role, never a model. Four roles: **triage** (cheap extraction), **judge** (fit score),
**writer** and **verifier**. Pick a profile with `--models <name>` or `GROUNDTRUTH_MODELS` in `.env`:

| Profile | Keys needed | Roughly per tailored application |
|---|---|---|
| `balanced` (default) | OpenAI, Gemini, Anthropic | $0.05 – 0.15 |
| `cheap` | Gemini, Anthropic | $0.01 – 0.03 |
| `anthropic` / `openai` | one | $0.05 – 0.20 |
| `local` | none (Ollama) | free, slower; quality depends on the model |

The default profile came from a blind test of 8 models across 3 vendors, judged by a cross-vendor panel with
code-checked answer sheets: the writer and the verifier are from different vendors, because a model is poor at
catching its own inventions. Any model [LiteLLM](https://docs.litellm.ai/docs/providers) supports works: copy
`groundtruth/models.yaml` into your workspace and edit it.

Every call is logged with its cost to `.groundtruth/ledger.sqlite` in your workspace, and
`GROUNDTRUTH_MONTHLY_BUDGET_USD` (default $10) stops paid calls once the month's spend reaches it.

## Privacy

- Your fact sheet, settings, keys and applications stay in your workspace folder. `groundtruth init` writes a
  `.gitignore` so they never end up in a repository by accident.
- The fact sheet and the posting are sent only to the model providers in the profile you choose. With the `local`
  profile nothing leaves your computer.
- Postings are treated as untrusted data: instructions hidden inside a posting are ignored.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The tests use fake models and a made-up person: no keys, no network, no cost.

## Background

Groundtruth CV is extracted from a personal multi-agent system I run daily for my own job and PhD search. The rest
of that system stays private; this is the part that is useful to anyone.

## Licence

MIT. See [LICENSE](LICENSE).
