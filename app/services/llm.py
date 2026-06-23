"""Thin LLM client wrapper. Logs every call to the LlmCall table.

Falls back to deterministic mock implementations when USE_MOCK_LLM=true or no API key is
configured. The mocks are written so the rest of the system runs and demos correctly without
network access — but each one is intentionally weaker than the real LLM, so the contrast is
visible in the AI Ops view.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings
from app.db import session_scope
from app.models import LlmCall, LlmModelConfig

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Rough cost estimates (USD per 1K tokens) for Claude 3.5 Haiku via OpenRouter; only used
# to give the AI Ops page a budget-feel signal — exact billing is not the point.
COST_PER_1K_IN = 0.0008
COST_PER_1K_OUT = 0.004


@dataclass
class LlmResult:
    parsed: dict[str, Any]
    raw_response: str
    model: str
    mocked: bool
    tokens_in: int
    tokens_out: int
    latency_ms: int
    cost_usd: float
    validation_ok: bool
    validation_error: str


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _resolve_model(purpose: str) -> tuple[str, float]:
    """DB config beats env beats default. Returns (model, temperature)."""
    settings = get_settings()
    with session_scope() as s:
        cfg = s.get(LlmModelConfig, purpose)
        if cfg:
            return cfg.model, cfg.temperature
    env_attr = f"{purpose}_model"
    env_model = getattr(settings, env_attr, "") if hasattr(settings, env_attr) else ""
    return (env_model or settings.openrouter_model), 0.1


def _log_call(*, purpose: str, prompt: str, result: LlmResult, related_posting_id: int | None) -> None:
    with session_scope() as s:
        s.add(
            LlmCall(
                purpose=purpose,
                model=result.model,
                mocked=result.mocked,
                prompt=prompt[:8000],
                response=result.raw_response[:8000],
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                validation_ok=result.validation_ok,
                validation_error=result.validation_error or "",
                related_posting_id=related_posting_id,
            )
        )


# OpenRouter status codes that are worth retrying. 4xx other than 429 is config (won't
# get better by waiting); 5xx + 429 are transient (provider hiccup, rate-limit window).
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 522, 524}
_RETRYABLE_EXC = (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.NetworkError)


def _call_openrouter(prompt: str, json_schema: dict | None, model: str, temperature: float) -> tuple[str, str, int, int, int]:
    """Call OpenRouter with exponential-backoff retry on transient failures.

    Retries 5xx, 429 (rate limit), 408 (request timeout), 425, and bare network errors
    up to 2 times with 0.8s → 2.4s waits. Does NOT retry 400/401/403 — those are config
    errors that won't get better by waiting.
    """
    settings = get_settings()
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    if json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "result", "strict": True, "schema": json_schema},
        }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }

    backoffs = [0.0, 0.8, 2.4]  # 3 attempts total (first is immediate)
    last_exc: Exception | None = None
    started = time.perf_counter()
    for attempt, wait in enumerate(backoffs):
        if wait:
            log.warning(
                "llm._run retry %d/%d after %.1fs: %s",
                attempt + 1, len(backoffs), wait, last_exc,
            )
            time.sleep(wait)
        try:
            with httpx.Client(timeout=30.0) as client:
                r = client.post(OPENROUTER_URL, json=body, headers=headers)
        except _RETRYABLE_EXC as e:
            last_exc = e
            continue  # network blip — try again
        # Permanent client errors — surface immediately, no retry.
        if 400 <= r.status_code < 500 and r.status_code not in _RETRYABLE_STATUS:
            body_excerpt = (r.text or "")[:600].strip()
            raise httpx.HTTPStatusError(
                f"{r.status_code} {r.reason_phrase} from OpenRouter (model={model}): {body_excerpt}",
                request=r.request,
                response=r,
            )
        if r.status_code in _RETRYABLE_STATUS and attempt < len(backoffs) - 1:
            last_exc = httpx.HTTPStatusError(
                f"{r.status_code} {r.reason_phrase} from OpenRouter (transient, attempt {attempt + 1}/{len(backoffs)})",
                request=r.request,
                response=r,
            )
            continue  # try again
        # Got a usable response (2xx) OR exhausted retries — break out of the loop.
        break
    else:
        # All attempts exhausted with retryable exceptions only.
        raise last_exc or RuntimeError("OpenRouter call failed for unknown reasons")

    latency = int((time.perf_counter() - started) * 1000)
    if r.status_code >= 400:
        body_excerpt = (r.text or "")[:600].strip()
        raise httpx.HTTPStatusError(
            f"{r.status_code} {r.reason_phrase} from OpenRouter (model={model}, after {attempt + 1} attempt(s)): {body_excerpt}",
            request=r.request,
            response=r,
        )
    data = r.json()
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return (
        content,
        model,
        int(usage.get("prompt_tokens", _approx_tokens(prompt))),
        int(usage.get("completion_tokens", _approx_tokens(content))),
        latency,
    )


def _should_mock() -> bool:
    s = get_settings()
    return s.use_mock_llm or not s.openrouter_api_key


# ------------------------- mock implementations -------------------------

_WAGE_RANGE_RE = re.compile(
    r"\$?\s*(?P<low>\d{1,3}(?:\.\d{1,2})?)\s*(?:[\-–—to]+)\s*\$?\s*(?P<high>\d{1,3}(?:\.\d{1,2})?)"
)
_WAGE_SINGLE_RE = re.compile(r"\$\s*(?P<v>\d{1,3}(?:\.\d{1,2})?)\s*(?:/?\s*hr|per\s+hour)?", re.IGNORECASE)


def _mock_extract(html: str, raw_title: str) -> dict[str, Any]:
    text = re.sub(r"<[^>]+>", " ", html)
    match = _WAGE_RANGE_RE.search(text)
    if match:
        lo = float(match.group("low"))
        hi = float(match.group("high"))
    else:
        singles = [float(m.group("v")) for m in _WAGE_SINGLE_RE.finditer(text)]
        singles = [v for v in singles if 5.0 < v < 100.0]
        if singles:
            lo, hi = min(singles), max(singles)
        else:
            lo, hi = 0.0, 0.0
    if hi < lo:
        lo, hi = hi, lo
    return {
        "wage_low": round(lo, 2),
        "wage_high": round(hi, 2),
        "wage_unit": "hourly",
        "role": raw_title,
        "confidence": 0.65 if lo and hi else 0.2,
        "reasoning": "regex-extracted in mock mode",
    }


_OUTDOOR_HINTS = (
    "warehouse", "fulfillment", "freight", "lot", "yard", "sortation", "receiving",
    "loader", "material handler", "cart", "stocker",
)
_INDOOR_HINTS = (
    "cashier", "barista", "office", "service deli", "customer service",
    "host", "supervisor", "returns", "self-checkout",
)

# Role-discovery substring rules. Kept narrower than the general _OUTDOOR_HINTS /
# _INDOOR_HINTS above on purpose: discovery is making a 3-way call (outdoor vs
# indoor vs not_relevant) where the general classifier only chose between 2. The
# explicit-only hits should be confident; everything else falls through to
# not_relevant so the operator's review queue isn't polluted with garbage.
_DISCOVERY_OUTDOOR = (
    "warehouse", "loader", "driver", "yard", "forklift", "stocker", "attendant", "outdoor",
)
_DISCOVERY_INDOOR = (
    "clerk", "cashier", "office", "customer", "claims", "title", "dispatch", "indoor",
)


def _mock_classify(raw_title: str) -> dict[str, Any]:
    t = raw_title.lower()
    bucket = "outdoor" if any(h in t for h in _OUTDOOR_HINTS) else (
        "indoor" if any(h in t for h in _INDOOR_HINTS) else "indoor"
    )
    normalized = "Material Handler" if bucket == "outdoor" else "Customer Service Associate"
    return {
        "normalized_role": normalized,
        "bucket": bucket,
        "confidence": 0.7,
        "reasoning": "keyword-matched in mock mode",
    }


def _mock_classify_titles_batch(titles: list[str], competitor_name: str) -> dict[str, Any]:
    """Deterministic batch classification used in mock mode + as a test harness.

    Each title gets bucket=outdoor/indoor/not_relevant via simple substring rules.
    Confidence mirrors the spec: 0.85 outdoor, 0.80 indoor, 0.50 not_relevant.
    Returned as a dict (not a list) so the same JSON-schema wrapper used for the
    other LLM purposes works without a list-typed top-level schema.
    """
    out: list[dict[str, Any]] = []
    for raw in titles:
        t = (raw or "").lower()
        if any(h in t for h in _DISCOVERY_OUTDOOR):
            out.append({
                "title": raw,
                "bucket": "outdoor",
                "confidence": 0.85,
                "reasoning": "substring matched an outdoor/warehouse hint in mock mode",
            })
        elif any(h in t for h in _DISCOVERY_INDOOR):
            out.append({
                "title": raw,
                "bucket": "indoor",
                "confidence": 0.80,
                "reasoning": "substring matched an indoor/office hint in mock mode",
            })
        else:
            out.append({
                "title": raw,
                "bucket": "not_relevant",
                "confidence": 0.50,
                "reasoning": "no outdoor or indoor hint matched in mock mode",
            })
    return {"classifications": out}


# Role-ish word allowlist for the mock web-extract fallback. We don't want
# every capitalized phrase a snippet contains ("The Home Depot", "Apply Today")
# — only ones that plausibly describe an hourly job. The regex below grabs
# 1–4-word Title-Cased spans; the allowlist then filters them down to actual
# role candidates. Deterministic, keyword-driven, offline.
_WEB_EXTRACT_TOKEN_RE = re.compile(
    r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b"
)
_WEB_EXTRACT_ROLE_HINTS = frozenset({
    "associate", "cashier", "driver", "loader", "clerk", "manager",
    "specialist", "operator", "attendant", "stocker", "handler", "barista",
    "supervisor", "assistant", "receiver", "picker", "packer", "filler",
})


def _mock_web_extract(snippets_blob: str) -> dict[str, Any]:
    """Regex-based mock for the web-extract LLM step. Pulls Title-Cased
    1–4-word spans whose lowercased last word looks like a role noun (from
    ``_WEB_EXTRACT_ROLE_HINTS``). Deduped; preserves first-seen order so
    re-runs over the same fixture are stable."""
    found: list[str] = []
    seen: set[str] = set()
    for m in _WEB_EXTRACT_TOKEN_RE.finditer(snippets_blob):
        candidate = m.group(0).strip()
        if not candidate:
            continue
        # Last word must be a known role-ish noun. This is what filters out
        # "The Home Depot" or "Apply Today" — neither ends in a role hint.
        last = candidate.rsplit(" ", 1)[-1].lower()
        if last not in _WEB_EXTRACT_ROLE_HINTS:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        found.append(candidate)
    return {"titles": found}


def _mock_narrative(facts: dict[str, Any]) -> dict[str, Any]:
    high = facts.get("highest_pressure_state", "—")
    low = facts.get("lowest_pressure_state", "—")
    nat_gap = facts.get("national_wage_gap", 0.0)
    body = (
        f"Across {facts.get('location_count', 0)} ACME locations, the national average wage gap to local "
        f"competitors is {nat_gap:+.2f}/hour. Pressure is highest in {high} where competitors out-pay ACME "
        f"by the largest margin; the lowest-pressure market is {low}. Outdoor roles "
        f"(warehouse/material-handler analogs) drive most of the gap. Costco and Amazon are the most "
        f"consistent upward pressure across markets."
    )
    return {"body": body}


# ------------------------- public APIs -------------------------

# OpenAI strict json_schema mode requires every property to be listed in `required`.
# Anthropic via OpenRouter ignores enforcement entirely. So we list ALL properties in
# the wire schema (keeps OpenAI happy) and apply a looser, server-side check via
# *_REQUIRED_KEYS below that only enforces load-bearing fields. Consumers fill defaults
# for the rest.
# NOTE: Anthropic's structured-output endpoint rejects ``minimum``/``maximum``
# constraints on ``"number"`` types (returns
# ``output_config.format.schema: For 'number' type, properties maximum, minimum
# are not supported``). OpenAI tolerates them but doesn't enforce them either —
# strict mode only enforces required + property shape. So we drop them from the
# wire schemas and clamp ``confidence`` server-side in :func:`_clamp_confidence`
# below.
WAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "wage_low": {"type": "number"},
        "wage_high": {"type": "number"},
        "wage_unit": {"type": "string", "enum": ["hourly", "annual"]},
        "role": {"type": "string"},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["wage_low", "wage_high", "wage_unit", "role", "confidence", "reasoning"],
}
WAGE_REQUIRED_KEYS = ("wage_low", "wage_high")

CLASSIFY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "normalized_role": {"type": "string"},
        "bucket": {"type": "string", "enum": ["outdoor", "indoor"]},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["normalized_role", "bucket", "confidence", "reasoning"],
}
CLASSIFY_REQUIRED_KEYS = ("bucket",)

NARRATIVE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"body": {"type": "string"}},
    "required": ["body"],
}
NARRATIVE_REQUIRED_KEYS = ("body",)

# Role-discovery batch schema. OpenAI strict mode requires a top-level object — so
# we wrap the actual list under a single "classifications" key instead of typing
# the response as a bare JSON array. Each list element follows the same
# {title, bucket, confidence, reasoning} shape spec'd in role_discovery.html's
# review queue. ``not_relevant`` is the third bucket that lets the LLM signal
# "this title isn't a yard or office role" — those rows can't be accepted.
ROLE_DISCOVERY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "bucket": {
                        "type": "string",
                        "enum": ["outdoor", "indoor", "not_relevant"],
                    },
                    "confidence": {"type": "number"},
                    "reasoning": {"type": "string"},
                },
                "required": ["title", "bucket", "confidence", "reasoning"],
            },
        }
    },
    "required": ["classifications"],
}
ROLE_DISCOVERY_REQUIRED_KEYS = ("classifications",)

# Role-discovery V2 — web-search snippet → distinct title extraction. The
# orchestrator hands the LLM ~5 search result snippets per call; the LLM
# returns the list of job-title strings mentioned in them, skipping generic
# phrases like "jobs", "careers", "apply now". Downstream of this, the
# extracted titles flow through ``classify_titles_batch`` exactly like V1's
# DB-mined titles do — so the rest of the bucket pipeline stays unchanged.
WEB_EXTRACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "titles": {
            "type": "array",
            "items": {"type": "string"},
        }
    },
    "required": ["titles"],
}
WEB_EXTRACT_REQUIRED_KEYS = ("titles",)

# Maps the wire schema → the keys we actually require client-side. We can't read this
# from `required` on the schema because we deliberately diverge there.
_REQUIRED_KEYS_BY_SCHEMA: dict[int, tuple[str, ...]] = {}


def _register_required(schema: dict, keys: tuple[str, ...]) -> dict:
    _REQUIRED_KEYS_BY_SCHEMA[id(schema)] = keys
    return schema


_register_required(WAGE_SCHEMA, WAGE_REQUIRED_KEYS)
_register_required(CLASSIFY_SCHEMA, CLASSIFY_REQUIRED_KEYS)
_register_required(NARRATIVE_SCHEMA, NARRATIVE_REQUIRED_KEYS)
_register_required(ROLE_DISCOVERY_SCHEMA, ROLE_DISCOVERY_REQUIRED_KEYS)
_register_required(WEB_EXTRACT_SCHEMA, WEB_EXTRACT_REQUIRED_KEYS)


_MARKDOWN_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _tolerant_json_parse(raw: str) -> dict:
    """Parse JSON robustly from a model response that may include code fences or prose.

    Anthropic via OpenRouter doesn't strictly enforce `response_format.json_schema`, so
    Haiku/Sonnet sometimes wrap their JSON in ```json ... ``` fences or add a sentence
    of prose before/after. A plain `json.loads()` then dies with `Expecting value` or
    `Extra data`. This walks down a ladder of progressively more forgiving extractions.
    """
    if not raw or not raw.strip():
        raise json.JSONDecodeError("empty response", raw or "", 0)
    s = raw.strip()
    # Strip a leading/trailing markdown code fence if present.
    s = _MARKDOWN_FENCE_RE.sub("", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Grab the largest balanced-looking {...} block. Greedy regex finds the span from
    # the first `{` to the last `}`, which is what we want for "prose then JSON" or
    # "JSON then trailing notes".
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        raise json.JSONDecodeError("no JSON object found in response", s, 0)
    return json.loads(m.group(0))


def _clamp_confidence(parsed: dict) -> None:
    """Coerce ``confidence`` (and any nested list-of-objects' confidence) to
    [0.0, 1.0] in-place. Wire schemas used to enforce these bounds via
    ``minimum``/``maximum``, but Anthropic's structured-output endpoint rejects
    those properties on ``"number"`` types — we now bound them server-side.
    """
    if not isinstance(parsed, dict):
        return
    if isinstance(parsed.get("confidence"), (int, float)):
        parsed["confidence"] = max(0.0, min(1.0, float(parsed["confidence"])))
    # Role-discovery batch shape: {"classifications": [{"confidence": ...}, …]}
    cls = parsed.get("classifications")
    if isinstance(cls, list):
        for row in cls:
            if isinstance(row, dict) and isinstance(row.get("confidence"), (int, float)):
                row["confidence"] = max(0.0, min(1.0, float(row["confidence"])))


def _validate(parsed: dict, schema: dict) -> tuple[bool, str]:
    required = _REQUIRED_KEYS_BY_SCHEMA.get(id(schema), tuple(schema.get("required", [])))
    for key in required:
        if key not in parsed:
            return False, f"missing key: {key}"
    _clamp_confidence(parsed)
    return True, ""


def _run(
    *,
    purpose: str,
    prompt: str,
    schema: dict,
    mock_fn,
    mock_args: tuple,
    related_posting_id: int | None,
) -> LlmResult:
    target_model_for_log, _ = _resolve_model(purpose)
    log.debug(
        "llm._run purpose=%s model=%s tokens_in≈%d",
        purpose, target_model_for_log, _approx_tokens(prompt),
    )

    if _should_mock():
        started = time.perf_counter()
        parsed = mock_fn(*mock_args)
        latency = int((time.perf_counter() - started) * 1000)
        raw = json.dumps(parsed)
        ok, err = _validate(parsed, schema)
        target_model, _ = _resolve_model(purpose)
        result = LlmResult(
            parsed=parsed,
            raw_response=raw,
            model=f"mock · would-use {target_model}",
            mocked=True,
            tokens_in=_approx_tokens(prompt),
            tokens_out=_approx_tokens(raw),
            latency_ms=latency,
            cost_usd=0.0,
            validation_ok=ok,
            validation_error=err,
        )
        _log_call(purpose=purpose, prompt=prompt, result=result, related_posting_id=related_posting_id)
        if not ok:
            # Used to be DB-only — mirror to the stream so /admin/logs can
            # show validation regressions without crossing to /admin/ai-ops.
            log.warning(
                "llm._run validation_error purpose=%s (mocked): %s",
                purpose, err,
            )
        else:
            log.info(
                "llm._run ok purpose=%s latency=%dms cost=$%.4f",
                purpose, result.latency_ms, result.cost_usd,
            )
        return result

    target_model, temperature = _resolve_model(purpose)
    # We want to preserve the raw API response even when JSON parsing fails — otherwise
    # the LlmCall log only contains the Python exception, which makes "why did this
    # fail?" debugging impossible. Capture raw first, then attempt to parse.
    raw = ""
    model = target_model
    t_in = _approx_tokens(prompt)
    t_out = 0
    latency = 0
    try:
        raw, model, t_in, t_out, latency = _call_openrouter(prompt, schema, target_model, temperature)
    except Exception as e:
        result = LlmResult(
            parsed={}, raw_response=raw or str(e), model=target_model, mocked=False,
            tokens_in=t_in, tokens_out=t_out, latency_ms=latency, cost_usd=0.0,
            validation_ok=False, validation_error=f"{type(e).__name__}: {e}",
        )
        _log_call(purpose=purpose, prompt=prompt, result=result, related_posting_id=related_posting_id)
        log.warning(
            "llm._run transport failure purpose=%s model=%s: %s: %s",
            purpose, target_model, type(e).__name__, e,
        )
        raise

    try:
        parsed = _tolerant_json_parse(raw)
        ok, err = _validate(parsed, schema)
    except Exception as e:  # parse failure — log with the actual raw response preserved
        cost = (t_in / 1000.0) * COST_PER_1K_IN + (t_out / 1000.0) * COST_PER_1K_OUT
        result = LlmResult(
            parsed={}, raw_response=raw, model=model, mocked=False,
            tokens_in=t_in, tokens_out=t_out, latency_ms=latency, cost_usd=cost,
            validation_ok=False, validation_error=f"{type(e).__name__}: {e}",
        )
        _log_call(purpose=purpose, prompt=prompt, result=result, related_posting_id=related_posting_id)
        log.warning(
            "llm._run parse failure purpose=%s model=%s: %s: %s",
            purpose, model, type(e).__name__, e,
        )
        raise

    cost = (t_in / 1000.0) * COST_PER_1K_IN + (t_out / 1000.0) * COST_PER_1K_OUT
    result = LlmResult(
        parsed=parsed, raw_response=raw, model=model, mocked=False,
        tokens_in=t_in, tokens_out=t_out, latency_ms=latency, cost_usd=cost,
        validation_ok=ok, validation_error=err,
    )
    _log_call(purpose=purpose, prompt=prompt, result=result, related_posting_id=related_posting_id)
    if not ok:
        log.warning(
            "llm._run validation_error purpose=%s model=%s: %s",
            purpose, model, err,
        )
    else:
        log.info(
            "llm._run ok purpose=%s latency=%dms cost=$%.4f",
            purpose, latency, cost,
        )
    return result


def extract_wage(html: str, raw_title: str, *, related_posting_id: int | None = None) -> LlmResult:
    prompt = (
        "Extract the entry-level wage range from this job posting HTML. "
        "If the posting gives a single starting wage, set wage_low = wage_high. "
        "Use hourly unless the posting is clearly annual. "
        "Respond with JSON matching the schema.\n\n"
        f"Raw title: {raw_title}\n\nHTML:\n{html[:6000]}"
    )
    return _run(
        purpose="extraction",
        prompt=prompt,
        schema=WAGE_SCHEMA,
        mock_fn=_mock_extract,
        mock_args=(html, raw_title),
        related_posting_id=related_posting_id,
    )


def classify_role(raw_title: str, *, related_posting_id: int | None = None) -> LlmResult:
    prompt = (
        "Classify this competitor job title into ACME's role taxonomy. "
        "Buckets: 'outdoor' (warehouse/yard/lot work) or 'indoor' (retail/cashier/office). "
        "Return the closest normalized role name and the bucket.\n\n"
        f"Title: {raw_title}"
    )
    return _run(
        purpose="classification",
        prompt=prompt,
        schema=CLASSIFY_SCHEMA,
        mock_fn=_mock_classify,
        mock_args=(raw_title,),
        related_posting_id=related_posting_id,
    )


def classify_titles_batch(
    titles: list[str], competitor_name: str
) -> list[dict[str, Any]]:
    """Batch-classify a list of raw job titles for the Role Discovery workflow.

    Returns a list of dicts shaped
    ``{"title", "bucket", "confidence", "reasoning"}`` — one per input title, in
    input order (best-effort: if the model drops a title we backfill it as
    ``not_relevant`` with confidence 0 so the caller always gets a 1:1 result).
    Each call writes one ``LlmCall`` row with ``purpose="role_discovery"`` so the
    AI Ops page surfaces it alongside extraction/classification/narrative.

    The empty-list short-circuit avoids burning a call on zero work — callers
    (the orchestrator) batch in ~20s and may have a degenerate empty batch.
    """
    if not titles:
        return []
    prompt = (
        "You are helping expand a wage-comparison scraper's keyword set. For each job "
        "title below, decide whether it represents an OUTDOOR role (yard / lot / "
        "warehouse / driver — entry-level physical work outside or in a warehouse), "
        "an INDOOR role (cashier / clerk / office / customer service — entry-level "
        "register or desk work), or NOT_RELEVANT (management, professional, software, "
        "anything not an entry-level frontline role). Respond with JSON matching the "
        "schema; one classification per title, preserving the input title string "
        "verbatim.\n\n"
        f"Competitor: {competitor_name}\n\n"
        "Titles:\n" + "\n".join(f"- {t}" for t in titles)
    )
    result = _run(
        purpose="role_discovery",
        prompt=prompt,
        schema=ROLE_DISCOVERY_SCHEMA,
        mock_fn=_mock_classify_titles_batch,
        mock_args=(titles, competitor_name),
        related_posting_id=None,
    )
    parsed = result.parsed.get("classifications", []) if result.parsed else []
    # Normalize into title-keyed map so we can backfill missing rows. The model
    # occasionally drops a title from a long batch — better to surface that
    # rather than crash the orchestrator on a missing element.
    by_title: dict[str, dict[str, Any]] = {}
    for row in parsed:
        if isinstance(row, dict) and isinstance(row.get("title"), str):
            by_title[row["title"]] = row
    out: list[dict[str, Any]] = []
    for t in titles:
        row = by_title.get(t)
        if row is None:
            out.append({
                "title": t,
                "bucket": "not_relevant",
                "confidence": 0.0,
                "reasoning": "LLM did not return a classification for this title",
            })
        else:
            bucket = row.get("bucket") if row.get("bucket") in {"outdoor", "indoor", "not_relevant"} else "not_relevant"
            try:
                conf = float(row.get("confidence", 0.0))
            except (TypeError, ValueError):
                conf = 0.0
            conf = max(0.0, min(1.0, conf))
            out.append({
                "title": t,
                "bucket": bucket,
                "confidence": conf,
                "reasoning": str(row.get("reasoning", "") or ""),
            })
    return out


def extract_titles_from_search_results(
    results: list[dict[str, str]], competitor_name: str
) -> list[str]:
    """Extract distinct job-title strings from a batch of web-search result snippets.

    Input: a list of ``{title, snippet, url}`` dicts (the contract returned by
    ``app.services.web_search.search``). The LLM reads them and returns the
    union of role titles mentioned, skipping generic phrases like "jobs" or
    "apply now". Each call writes one ``LlmCall`` row with
    ``purpose='role_discovery_web_extract'`` so the AI Ops page surfaces V2
    alongside the rest of the pipeline.

    Returns a deduped, order-preserving list of title strings. Empty input
    short-circuits to ``[]`` (no LLM call) to keep zero-result searches free.
    """
    if not results:
        return []
    # Build a compact bulleted blob from the snippets — title + snippet text
    # are what actually carry role-title signal. URLs help dedupe at the
    # caller level but aren't useful to the extractor here.
    lines = []
    for r in results:
        title = (r.get("title") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        if title or snippet:
            lines.append(f"- {title}\n  {snippet}")
    snippets_blob = "\n".join(lines)
    prompt = (
        "Given these web search result snippets about a competitor's job "
        "postings, extract the list of distinct job title strings mentioned. "
        "Return JSON: {titles: [string, ...]}. Skip generic phrases like "
        "'jobs', 'careers', 'apply now', 'hiring'. Real titles only — names "
        "of specific roles you would see on a careers page (e.g. 'Cashier', "
        "'Warehouse Associate', 'Forklift Driver').\n\n"
        f"Competitor: {competitor_name}\n\n"
        "Search results:\n" + snippets_blob
    )
    result = _run(
        purpose="role_discovery_web_extract",
        prompt=prompt,
        schema=WEB_EXTRACT_SCHEMA,
        mock_fn=_mock_web_extract,
        mock_args=(snippets_blob,),
        related_posting_id=None,
    )
    raw_titles = result.parsed.get("titles", []) if result.parsed else []
    out: list[str] = []
    seen: set[str] = set()
    for t in raw_titles:
        if not isinstance(t, str):
            continue
        cleaned = t.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def generate_narrative(facts: dict[str, Any]) -> LlmResult:
    prompt = (
        "Write a single tight executive paragraph (3–4 sentences) summarizing competitive wage pressure on ACME. "
        "Use only the facts provided — do not invent. Mention the highest-pressure and lowest-pressure markets, "
        "the national gap, and which employer drives the pressure most.\n\n"
        f"FACTS:\n{json.dumps(facts, indent=2)}"
    )
    return _run(
        purpose="narrative",
        prompt=prompt,
        schema=NARRATIVE_SCHEMA,
        mock_fn=_mock_narrative,
        mock_args=(facts,),
        related_posting_id=None,
    )
