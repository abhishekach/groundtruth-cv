"""One door to every model.

    from groundtruth.llm import ask
    fit = ask("judge", prompt, system=..., schema=Fit)      # -> a validated Fit instance

- Code names a ROLE (triage, judge, writer, verifier), never a model. Roles map to models in models.yaml.
- With `schema`, the answer must be JSON matching the Pydantic model. Plan: the model's native structured output
  (OpenAI and Gemini only), then the schema in the prompt, then the backup model. Every attempt is validated; a call
  that never validates fails loudly instead of returning something half-right.
- Every call is logged to <workspace>/.groundtruth/ledger.sqlite (role, model, tokens, cost, time), and the
  month-to-date spend is checked against GROUNDTRUTH_MONTHLY_BUDGET_USD before every paid call.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)
ROLES_NEEDED = ("triage", "judge", "writer", "verifier")
LOCAL_PREFIXES = ("ollama/", "ollama_chat/", "lm_studio/")
NATIVE_PREFIXES = ("openai/", "gemini/")   # Anthropic gets the schema in the prompt (faster, same result once validated)
SCHEMA_MIN_TOKENS = 2500                   # reasoning models think first; a tight cap can leave the JSON empty
THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class BudgetExceeded(RuntimeError):
    pass


class _State:
    workspace: Path = Path.cwd()
    roles: dict[str, dict] = {}
    router = None
    run_cost: float = 0.0
    native_refused: set = set()


S = _State()


# ---------------------------------------------------------------- configuration
def load_profile(workspace: Path, name: str | None = None) -> dict[str, dict]:
    """Role -> {model, backup?, api_base?}. A models.yaml in the workspace wins over the packaged one."""
    own = workspace / "models.yaml"
    path = own if own.exists() else Path(__file__).with_name("models.yaml")
    cfg = yaml.safe_load(path.read_text())
    name = name or os.environ.get("GROUNDTRUTH_MODELS") or "balanced"
    profiles = cfg.get("profiles") or {}
    if name not in profiles:
        raise ValueError(f"Model profile '{name}' not found in {path}. Choose one of: {', '.join(profiles)}")
    roles = profiles[name]
    missing = [r for r in ROLES_NEEDED if r not in roles]
    if missing:
        raise ValueError(f"Profile '{name}' in {path} has no model for: {', '.join(missing)}")
    return roles


def configure(workspace: Path, profile: str | None = None) -> None:
    """Called once by the CLI. Tests may skip it and replace _complete instead."""
    S.workspace = Path(workspace)
    S.roles = load_profile(S.workspace, profile)
    S.router = None


def _router():
    if S.router is None:
        import litellm
        from litellm import Router
        litellm.suppress_debug_info = True
        litellm.drop_params = True
        for n in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
            logging.getLogger(n).setLevel(logging.CRITICAL)   # failures go to the ledger instead
        model_list, fallbacks = [], []
        for role, r in S.roles.items():
            params = {"model": r["model"], **({"api_base": r["api_base"]} if r.get("api_base") else {})}
            model_list.append({"model_name": role, "litellm_params": params})
            if r.get("backup"):
                model_list.append({"model_name": f"{role}-backup", "litellm_params": {"model": r["backup"]}})
                fallbacks.append({role: [f"{role}-backup"]})
        S.router = Router(model_list=model_list, fallbacks=fallbacks, num_retries=1, timeout=180)
    return S.router


def model_of(role: str) -> str:
    return str((S.roles.get(role) or {}).get("model", ""))


def is_local(role: str) -> bool:
    return model_of(role).startswith(LOCAL_PREFIXES)


# ---------------------------------------------------------------- cost ledger
def _db() -> sqlite3.Connection:
    p = S.workspace / ".groundtruth" / "ledger.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=10)
    con.execute("create table if not exists calls(ts text, task text, role text, model text, in_tok integer, "
                "out_tok integer, cost_usd real, ms integer, ok integer, note text)")
    if "cost_kind" not in {r[1] for r in con.execute("pragma table_info(calls)")}:
        try:     # exact = the provider's price list; estimated = a price we had to assume
            con.execute("alter table calls add column cost_kind text")
        except sqlite3.OperationalError:
            pass
    return con


def _pending() -> Path:
    return S.workspace / ".groundtruth" / "ledger_pending.jsonl"


# ---------------------------------------------------------------- budget
# Before EVERY attempt (not once per answer), month-to-date spend + this attempt's worst case (prompt + max_tokens at
# list price) must stay under GROUNDTRUTH_MONTHLY_BUDGET_USD, and this command's spend under GROUNDTRUTH_RUN_BUDGET_USD.
# Providers can bill slightly differently, so the cap is firm but not exact to the cent.
UNKNOWN_PRICE_PER_M = (float(os.environ.get("GROUNDTRUTH_UNKNOWN_PRICE_IN_PER_M", "5")),
                       float(os.environ.get("GROUNDTRUTH_UNKNOWN_PRICE_OUT_PER_M", "25")))


def _prices(model: str) -> tuple[float, float, bool]:
    """(input $/token, output $/token, known). An unknown model gets a deliberately high price, never $0."""
    try:
        import litellm
        table = getattr(litellm, "model_cost", None) or {}
    except Exception:  # noqa: BLE001
        table = {}
    for k in (model, str(model).split("/", 1)[-1]):
        info = table.get(k)
        if info and info.get("input_cost_per_token") is not None and info.get("output_cost_per_token") is not None:
            return float(info["input_cost_per_token"]), float(info["output_cost_per_token"]), True
    return UNKNOWN_PRICE_PER_M[0] / 1e6, UNKNOWN_PRICE_PER_M[1] / 1e6, False


def estimate_cost(model: str, in_tok: int, out_tok: int) -> float:
    p_in, p_out, _ = _prices(model)
    return in_tok * p_in + out_tok * p_out


def _check_budget(role: str, messages: list, max_tokens: int, fallback: bool) -> None:
    if is_local(role):
        return
    in_tok = sum(len(str(m.get("content") or "")) for m in messages) // 3 + 50
    r = S.roles.get(role) or {}
    models = [model_of(role)] + ([r["backup"]] if fallback and r.get("backup") else [])
    est = max(estimate_cost(m, in_tok, max_tokens) for m in models)
    month = float(os.environ.get("GROUNDTRUTH_MONTHLY_BUDGET_USD", "10"))
    run = float(os.environ.get("GROUNDTRUTH_RUN_BUDGET_USD", "2"))
    spent = month_spend()
    if spent + est > month:
        raise BudgetExceeded(f"This month's spend ${spent:.2f} plus this call's worst case ${est:.2f} would pass the "
                             f"${month:.2f} cap (raise GROUNDTRUTH_MONTHLY_BUDGET_USD to continue)")
    if S.run_cost + est > run:
        raise BudgetExceeded(f"This command would pass its ${run:.2f} cap (GROUNDTRUTH_RUN_BUDGET_USD); stopped before the next call")


def month_spend() -> float:
    start = datetime.now(timezone.utc).strftime("%Y-%m-01")
    with _db() as con:
        (v,) = con.execute("select coalesce(sum(cost_usd),0) from calls where ts >= ?", (start,)).fetchone()
    return float(v)


def month_summary() -> list[tuple]:
    start = datetime.now(timezone.utc).strftime("%Y-%m-01")
    with _db() as con:
        return con.execute("select task, role, model, count(*), sum(cost_usd) from calls where ts >= ? "
                           "group by task, role, model order by sum(cost_usd) desc", (start,)).fetchall()


def _log(task, role, model, in_tok, out_tok, cost, ms, ok, note="", kind="exact") -> None:
    S.run_cost += cost
    row = (datetime.now(timezone.utc).isoformat(timespec="seconds"), task, role, model, in_tok, out_tok, cost, ms,
           int(ok), note[:500], kind)
    sql = ("insert into calls(ts, task, role, model, in_tok, out_tok, cost_usd, ms, ok, note, cost_kind) "
           "values (?,?,?,?,?,?,?,?,?,?,?)")
    try:
        with _db() as con:
            con.execute(sql, row)
            p = _pending()
            if p.exists():              # calls that could not be written earlier
                lines = p.read_text(encoding="utf-8").splitlines()
                p.unlink()
                for ln in lines:
                    if ln.strip():
                        con.execute(sql, tuple(json.loads(ln)))
    except sqlite3.Error:   # never lose a paid call: it waits in a file and is written with the next one
        with open(_pending(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------- calls
def _complete(role: str, messages: list, temperature: float, max_tokens: int, response_format=None, fallback: bool = True):
    kw = {"response_format": response_format} if response_format is not None else {}
    if not fallback:
        kw["fallbacks"] = []   # surface this model's own error instead of silently switching to the backup
    return _router().completion(model=role, messages=messages, temperature=temperature, max_tokens=max_tokens, **kw)


def _cost(resp, model: str) -> tuple[float, str]:
    """(cost, 'exact' | 'estimated'). An unknown price is estimated from the tokens, never logged as $0."""
    u = resp.usage
    try:
        import litellm
        c = float(litellm.completion_cost(completion_response=resp) or 0)
    except Exception:  # noqa: BLE001
        c = None
    if c is None or (c == 0 and (u.prompt_tokens or 0) + (u.completion_tokens or 0) > 0):
        return estimate_cost(model, u.prompt_tokens or 0, u.completion_tokens or 0), "estimated"
    return c, "exact"


def _json_from(text: str) -> str:
    text = text.strip()
    fences = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fences:
        return fences[-1].strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=0)
    return text[start:]


def _schema_text(schema) -> str:
    return "\n\nReply with JSON only, no prose, matching this JSON schema:\n" + json.dumps(schema.model_json_schema())


def _bad_request(e: Exception) -> bool:
    return type(e).__name__ == "BadRequestError"


def ask(role: str, prompt: str, *, system: str | None = None, schema: type[T] | None = None, task: str = "adhoc",
        max_tokens: int = 2000, temperature: float = 0.2) -> str | T:
    """One answer from a role; with `schema`, a validated instance of it."""
    if role not in S.roles:
        raise ValueError(f"Unknown role '{role}'. Known: {', '.join(sorted(S.roles)) or 'none (run configure first)'}")
    local = is_local(role)
    budget = float(os.environ.get("GROUNDTRUTH_MONTHLY_BUDGET_USD", "10"))
    if not local and month_spend() >= budget:
        raise BudgetExceeded(f"This month's model spend has reached the ${budget:.2f} cap "
                             "(raise GROUNDTRUTH_MONTHLY_BUDGET_USD to continue)")
    sys_text = system or "Use only the information given. If something is not in the input, say it is unknown."
    if schema is None:
        plan = [("plain", True)]
    else:
        max_tokens = max(max_tokens, SCHEMA_MIN_TOKENS)
        native = model_of(role).startswith(NATIVE_PREFIXES) and (role, schema.__name__) not in S.native_refused
        plan = ([("native", False)] if native else []) + [("prompt", False), ("prompt", True)]

    last: Exception | None = None
    for mode, fallback in plan:
        messages = [{"role": "system", "content": sys_text + (_schema_text(schema) if mode == "prompt" else "")},
                    {"role": "user", "content": prompt}]
        mt = max_tokens
        for attempt in (1, 2):
            _check_budget(role, messages, mt, fallback)      # before every attempt; raises BudgetExceeded
            t0 = time.time()
            try:
                resp = _complete(role, messages, temperature, mt, schema if mode == "native" else None, fallback)
            except Exception as e:  # noqa: BLE001  provider error, refused schema, rate limit ...
                el = time.time() - t0
                # a call that failed after a long wait may still have billed its prompt: count it as an estimate
                est = 0.0 if local or el < 20 else estimate_cost(model_of(role), sum(len(str(m["content"])) for m in messages) // 3, 0)
                _log(task, role, model_of(role), 0, 0, est, int(el * 1000), False, f"{mode}: {type(e).__name__}: {e}",
                     kind="estimated" if est else "exact")
                if mode == "native" and _bad_request(e):
                    S.native_refused.add((role, schema.__name__))
                last = e
                break
            ms = int((time.time() - t0) * 1000)
            used = getattr(resp, "model", None) or model_of(role)
            cost, kind = (0.0, "exact") if local else _cost(resp, used)
            u = resp.usage
            text = THINK_RE.sub("", resp.choices[0].message.content or "").strip()
            finish = getattr(resp.choices[0], "finish_reason", None)
            if schema is None:
                _log(task, role, used, u.prompt_tokens, u.completion_tokens, cost, ms, True, kind=kind)
                return text
            try:
                obj = schema.model_validate_json(_json_from(text))
                _log(task, role, used, u.prompt_tokens, u.completion_tokens, cost, ms, True, mode, kind=kind)
                return obj
            except ValidationError as e:
                _log(task, role, used, u.prompt_tokens, u.completion_tokens, cost, ms, False,
                     f"{mode}: finish={finish} schema: {str(e)[:300]}", kind=kind)
                last = e
                if finish == "length" or not text:
                    mt *= 2        # ran out of room: same request with more
                    continue
                if attempt == 1:
                    messages = messages + [{"role": "assistant", "content": text},
                                           {"role": "user", "content": f"That did not match the schema: {e}. "
                                                                       "Reply again with valid JSON only."}]
    assert last is not None
    raise last
