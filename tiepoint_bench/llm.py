"""The LLM arm: the same documents, and the model's judgement.

The model is given the documents as text and the firm's checklist, and asked what is
wrong. It is given **no help** the rules do not get: the same policy document, the
same figures, the same question. The comparison is the decision path.

The call is an OpenAI-compatible chat completion, at temperature 0, with the schema
carried in the prompt (a schema the model ignores is not a contract). Every call is
logged — the model, the prompt hash, the tokens, the latency, the outcome — because a
benchmark whose runs are invisible is a claim, not a benchmark.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .checks import Doc, Finding

CHECKLIST = (
    "You are an audit reviewer checking a synthetic fixed-asset register for FY2026. "
    "The documents are given as text. Find every error the documents support, and "
    "nothing else. Check, in this order:\n"
    "  dep_variance — the depreciation charge for an asset class is not the policy "
    "charge (the rate in the policy document x the class's closing cost);\n"
    "  cost_variance — the ledger (the GL extract or the trial balance) does not agree "
    "with the register's closing cost for an asset class;\n"
    "  rollforward_variance — the register's rollforward does not balance: opening + "
    "additions - disposals is not the closing cost;\n"
    "  prior_year_variance — the register's opening balance is not the prior year's "
    "closing balance;\n"
    "  missing_support — an addition in the register has no supporting invoice;\n"
    "  support_variance — the invoice for an addition states a different amount from "
    "the register;\n"
    "  missing_approval — a disposal in the register has no approved disposal document.\n"
    "One finding per error. Give the asset class or the asset reference the error is "
    "about, the amount if the documents state one, and where you saw it. If you find "
    "no error, return an empty list. Do not report a problem you cannot point at."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string",
                             "enum": ["dep_variance", "cost_variance",
                                      "rollforward_variance", "prior_year_variance",
                                      "missing_support", "support_variance",
                                      "missing_approval"]},
                    "subject": {"type": "string"},
                    "amount": {"type": "number"},
                    "where": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["type", "subject"],
            },
        }
    },
    "required": ["findings"],
}


def render_documents(docs: list[Doc]) -> str:
    """The documents as text — what a reviewer reads, in the same order every time."""
    from . import workbooks as tables
    from .text import text_of

    out: list[str] = []
    for doc in docs:
        if Path(doc.name).suffix.lower() in (".xlsx", ".xlsm", ".xls"):
            table = tables.read_table(doc.path, doc.name)
            chunks = []
            for sheet in table.sheets:
                rows = [" | ".join(c.text for c in row) for row in sheet.rows]
                chunks.append(f"=== FILE: {doc.name} (sheet: {sheet.name}) ===\n"
                              + "\n".join(rows))
            out.append("\n\n".join(chunks))
        else:
            out.append(f"=== FILE: {doc.name} ===\n{text_of(doc.path, Path(doc.name).suffix.lower())}")
    return "\n\n".join(out)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _parse_json(raw: str) -> dict:
    """Parse a model reply into JSON, tolerating fences and surrounding prose."""
    import re

    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in model reply")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("no JSON object found in model reply")


def _findings_from(result: dict) -> list[Finding]:
    from decimal import Decimal

    findings: list[Finding] = []
    for raw in (result.get("findings") or []):
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or "").strip()
        subject = str(raw.get("subject") or "").strip()
        if not kind or not subject:
            continue
        where = str(raw.get("where") or "").strip()
        amount = raw.get("amount")
        findings.append(Finding(
            kind=kind, subject=subject,
            detail=str(raw.get("note") or "").strip() or f"{kind}: {subject}",
            amount=(Decimal(str(amount)) if isinstance(amount, (int, float)) else None),
            locator={"kind": "text", "file": "model finding", "label": where} if where else None,
            source="model"))
    return findings


def _call(endpoint: str, api_key: str, model: str, text: str, timeout: float,
          max_tokens: int | None = None) -> dict:
    """One chat completion. Returns the parsed body and the usage."""
    import httpx

    prompt = (json.dumps({"task": "benchmark_fixed_asset_errors", "document": text},
                         default=str)
              + "\n\nReturn ONLY a JSON object matching this schema, no prose, no "
                "markdown fences:\n" + json.dumps(SCHEMA))
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": CHECKLIST},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    if max_tokens:
        body["max_tokens"] = max_tokens
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    started = time.time()
    with httpx.Client(timeout=timeout) as client:
        r = client.post(f"{endpoint.rstrip('/')}/chat/completions", json=body, headers=headers)
        r.raise_for_status()
        payload = r.json()
    content = payload["choices"][0]["message"]["content"]
    usage = payload.get("usage") or {}
    return {
        "parsed": _parse_json(content),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "latency_ms": int((time.time() - started) * 1000),
        "prompt_sha256": _sha(prompt),
        "model": model,
    }


def run_llm_arm(population, model: str | None = None, provider: str | None = None,
                endpoint: str | None = None, api_key: str | None = None,
                timeout: float = 600.0, max_tokens: int | None = None):
    """Run the model over the population and score it."""
    from decimal import Decimal

    from .__main__ import ArmResult, _tally, docs_for

    provider = provider or "cloud"
    if endpoint is None:
        endpoint = (os.getenv("AI_BASE_URL", "https://ollama.com/v1") if provider == "cloud"
                    else os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))
    if api_key is None:
        api_key = os.getenv("AI_API_KEY", "")
    model = model or (os.getenv("AI_MODEL", "glm-5.3-flash") if provider == "cloud"
                      else os.getenv("OLLAMA_MODEL", "gemma4:latest"))
    if max_tokens is None:
        max_tokens = int(os.getenv("MAX_TOKENS", "0")) or None

    result = ArmResult(arm="llm", model=model)
    for fixture in population.fixtures:
        docs = docs_for(fixture)
        text = render_documents(docs)
        started = time.time()
        out = None
        last_error = None
        # One retry. An empty answer is usually transient — the model spent its
        # budget thinking and the endpoint returned a message with no content —
        # and giving up on the first one turns a transient event into a permanent
        # loss. On the 40-fixture population that difference is not marginal.
        for attempt in range(2):
            try:
                out = _call(endpoint, api_key, model, text, timeout, max_tokens)
                break
            except Exception as exc:  # noqa: BLE001 — a failed call is a failed fixture
                last_error = exc
                if attempt == 0:
                    continue
        if out is None:
            elapsed = time.time() - started
            result.failed_fixtures += 1
            result.times.append(elapsed)
            result.seeded += len(fixture.errors)
            result.missed += len(fixture.errors)
            result.per_fixture.append({
                "fixture": fixture.id, "seeded": len(fixture.errors), "found": 0,
                "missed": len(fixture.errors), "false_positives": 0, "duplicates": 0,
                "seconds": round(elapsed, 3), "error": str(last_error)[:200]})
            continue
        elapsed = time.time() - started
        result.times.append(elapsed)
        result.model_calls = getattr(result, "model_calls", 0) + 1
        result.prompt_tokens = getattr(result, "prompt_tokens", 0) + (out["prompt_tokens"] or 0)
        result.completion_tokens = (getattr(result, "completion_tokens", 0)
                                    + (out["completion_tokens"] or 0))
        findings = _findings_from(out["parsed"])
        _tally(result, fixture, findings, elapsed)
    return result
