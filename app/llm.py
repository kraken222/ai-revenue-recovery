"""Anthropic client wrapper for the three places this system uses an LLM.

Scope discipline, restated because it is the whole architecture: the LLM never
decides whether or when to move money. It does three narrow jobs —

  1. read an unstructured bank/PSP decline narration into the structured taxonomy,
  2. draft the customer-facing recovery message,
  3. render a decision trace into one plain-English sentence for the audit log,

— and the compliance core, the bandit and the EV gate decide everything else. A
misclassification here can only ever route a payment to a DIFFERENT compliant
action, or to human review; it can never produce a non-compliant one.

Offline by default. The project's stated constraint is that everything runs with
zero external services, so `available()` is false without credentials and every
caller has a deterministic fallback. Absent an API key the classifier routes
rule-table misses to human review exactly as it did before this module existed.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"

# Classification is a short, bounded task: cap output hard and run at low effort.
# Thinking stays on (adaptive, the Opus 5 default) — disabling it on this model can
# leak reasoning into visible text, and low effort is the cheaper lever anyway.
_CLASSIFY_MAX_TOKENS = 512
_COPY_MAX_TOKENS = 1024


@lru_cache(maxsize=1)
def _client():
    """None whenever the SDK or credentials are absent, which is the normal state
    for a local run. Cached because credential resolution touches disk."""
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        # An `ant auth login` profile would also work, but probing for one on every
        # import is not worth the startup cost in a payment worker.
        return None
    try:
        import anthropic
    except ImportError:
        log.info("anthropic SDK not installed; LLM stages disabled")
        return None
    try:
        return anthropic.Anthropic()
    except Exception as exc:  # credential resolution can raise, not just return None
        log.warning("anthropic client unavailable, LLM stages disabled: %s", exc)
        return None


def available() -> bool:
    return _client() is not None


def reset_cache() -> None:
    """Tests flip credentials between cases; the client is memoised, so it needs a
    way to be re-resolved."""
    _client.cache_clear()


@dataclass
class LLMCall:
    """What one completion returned, plus enough provenance for the audit log to
    reconstruct why the system believed it."""

    ok: bool
    data: dict | None = None
    text: str = ""
    error: str | None = None
    usage: dict = field(default_factory=dict)


def complete_json(system: str, prompt: str, schema: dict, max_tokens: int = _CLASSIFY_MAX_TOKENS) -> LLMCall:
    """One structured completion. Never raises — a payment pipeline must not fall
    over because an inference call timed out, and every caller already has a
    conservative path for `ok=False`."""
    client = _client()
    if client is None:
        return LLMCall(ok=False, error="llm_unavailable")

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": schema},
            },
        )
    except Exception as exc:
        log.warning("LLM call failed: %s", exc)
        return LLMCall(ok=False, error=f"{type(exc).__name__}: {exc}")

    # A safety decline is a valid outcome, not an exception; treat it as a miss so
    # the caller falls through to human review.
    if response.stop_reason == "refusal":
        return LLMCall(ok=False, error="refusal")

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return LLMCall(ok=False, error="unparseable_json", text=text)

    return LLMCall(
        ok=True,
        data=data,
        text=text,
        usage={
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    )


def complete_text(system: str, prompt: str, max_tokens: int = _COPY_MAX_TOKENS) -> LLMCall:
    client = _client()
    if client is None:
        return LLMCall(ok=False, error="llm_unavailable")

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low"},
        )
    except Exception as exc:
        log.warning("LLM call failed: %s", exc)
        return LLMCall(ok=False, error=f"{type(exc).__name__}: {exc}")

    if response.stop_reason == "refusal":
        return LLMCall(ok=False, error="refusal")

    text = next((b.text for b in response.content if b.type == "text"), "").strip()
    return LLMCall(ok=bool(text), text=text, error=None if text else "empty_response")
