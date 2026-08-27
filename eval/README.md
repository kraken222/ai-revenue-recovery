# Classification eval set

A labelled set for measuring the two-tier classifier in [`app/classifier.py`](../app/classifier.py):
the rule table ([`taxonomy.py`](../app/taxonomy.py)) and the self-consistency LLM
ensemble ([`llm_classifier.py`](../app/llm_classifier.py)) that handles what the table
misses.

```bash
python -m scripts.eval_classifier            # rule tier: offline, deterministic
python -m scripts.eval_classifier --llm      # both tiers; needs ANTHROPIC_API_KEY
```

## What this set is not

**These narrations are hand-authored, not sampled from real Razorpay traffic.** They
are written against published Razorpay error-code documentation and the wire formats
Indian banks and UPI PSPs use, but no row here came off a production webhook.

So the numbers this harness prints measure **whether the classifier behaves as
specified on a set built to probe its decision boundary.** They are *not* an estimate
of accuracy on live traffic, and quoting them as one would be exactly the move this
project exists to refuse. A real accuracy estimate needs a labelled sample of actual
declines, drawn from the merchant's own error-code distribution — which is what
[README § Known limitations](../README.md#known-limitations) says is missing, and still
is.

What the set *can* honestly establish:

1. **Coverage** — what share of a realistic error-code population the rule table
   resolves without reaching for a model.
2. **Safety of misses** — that everything the table cannot resolve routes to a human
   rather than to a guess.
3. **Action-equivalence of errors** — whether a misclassification would actually have
   changed what the system did, or whether it lands in the same compliant action set.
4. **Ensemble behaviour on ambiguity** — that genuinely ambiguous narrations split the
   vote and escalate, instead of being answered confidently.

## Files

| File | Rows | Purpose |
|---|---|---|
| `error_codes.jsonl` | structured `(rail, code)` pairs | Rule-tier coverage and robustness |
| `narrations.jsonl` | free-text bank/PSP narration | LLM-tier precision/recall |

## Schema

```json
{
  "id": "ec001",
  "rail": "card",
  "code": "expired_card",
  "label": "hard_decline",
  "in_table": true,
  "difficulty": "clean",
  "note": "why this row exists"
}
```

- `label` — the category a domain expert assigns. `unknown` means **no confident
  answer is defensible**, so escalation is the *correct* outcome, not a miss.
- `in_table` — whether `taxonomy.lookup` is expected to resolve it. Rows with
  `in_table: true` are correct **by construction** (the label is read off the same
  table), so their precision is definitional and the harness labels it as such. The
  informative rows are the ones with `in_table: false`.
- `difficulty` — `clean` (one defensible answer), `ambiguous` (spans categories),
  `adversarial` (truncated, mis-cased, or malformed).

## Why "action-equivalent" is the metric that matters here

Not every misclassification is equally bad, and treating them as equal would misreport
the risk. `CATEGORY_ALLOWED_ACTIONS` in `taxonomy.py` maps both `soft_decline` and
`technical` to `["retry_now", "retry_at"]` — so confusing those two is **benign**: the
system does the same thing either way. Confusing `soft_decline` with `hard_decline`
is **action-changing**: one retries the instrument, the other asks the customer to
re-register.

The harness therefore splits errors three ways:

| | meaning | cost |
|---|---|---|
| `safe_miss` | a defensible label existed; the classifier escalated instead | efficiency — a human does work a machine could have |
| `benign_error` | wrong category, **same** compliant action set | none, in effect |
| `action_changing_error` | wrong category, **different** compliant action set | correctness — the system would have done the wrong thing |

`action_changing_error` is the number to judge on. A classifier with mediocre raw
accuracy but zero action-changing errors is doing its job; one with high accuracy whose
errors all cross action boundaries is not.
