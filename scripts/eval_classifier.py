"""Measure the two-tier classifier against a labelled set.

    python -m scripts.eval_classifier            # rule tier only: offline, deterministic
    python -m scripts.eval_classifier --llm      # both tiers; requires ANTHROPIC_API_KEY

The set lives in `eval/` and is **hand-authored, not sampled from real Razorpay
traffic** - see `eval/README.md`. What this harness reports is whether the classifier
behaves as specified on a set built to probe its decision boundary. It is not an
accuracy estimate for live traffic, and this script prints that caveat every run so a
number lifted out of its output carries it too.

### Why abstention is scored separately from error

This classifier is allowed to refuse. Everything it cannot resolve becomes UNKNOWN,
which compliance maps to `escalate_human` under COMP-002 - so a miss costs a human's
time, while a confident wrong answer can send the wrong action to a real customer.
Collapsing both into "accuracy" would score the safe failure and the dangerous one
identically, and would reward a classifier that guesses over one that escalates.

So errors are split three ways, using `CATEGORY_ALLOWED_ACTIONS` as the arbiter of
whether a mistake would actually have changed what the system did:

  safe_miss              a defensible label existed; the classifier escalated instead
  benign_error           wrong category, SAME compliant action set (soft <-> technical)
  action_changing_error  wrong category, DIFFERENT action set - the real failure

`action_changing_error` is the number to judge on.

### On rows the rule table defines

Rows marked `in_table: true` carry a label read off the same dict the classifier
consults, so of course it gets them right. That is definitional, not evidence, and the
harness segregates them rather than folding them into a headline accuracy that would
be inflated by construction. The informative rows are `in_table: false`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import llm  # noqa: E402
from app.classifier import classify  # noqa: E402
from app.models import Category, Rail  # noqa: E402
from app.taxonomy import CATEGORY_ALLOWED_ACTIONS  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"
REAL_CATEGORIES = ["soft_decline", "hard_decline", "technical", "risk_block"]


def load(name: str) -> list[dict]:
    path = EVAL_DIR / name
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def action_set(category: str) -> tuple[str, ...]:
    """What the system would actually be permitted to do, given this category.

    Two categories that map to the same action set are interchangeable as far as
    behaviour goes - confusing them changes the label in the audit trail but not the
    money action, and scoring them as equivalent to a hard/soft confusion would
    overstate the risk.
    """
    try:
        return tuple(CATEGORY_ALLOWED_ACTIONS[Category(category)])
    except (ValueError, KeyError):
        return ("escalate_human",)


def grade(truth: str, predicted: str) -> str:
    """One row's outcome. `unknown` is a legitimate *label*, meaning no confident
    answer is defensible - so escalating on one of those is correct, not a miss."""
    if truth == "unknown":
        return "correct_abstention" if predicted == "unknown" else "overconfident"
    if predicted == "unknown":
        return "safe_miss"
    if predicted == truth:
        return "correct"
    return "benign_error" if action_set(predicted) == action_set(truth) else "action_changing_error"


def prf(rows: list[tuple[str, str]]) -> dict[str, dict]:
    """Per-class precision / recall / F1 over the four real categories.

    Abstentions are counted as "not predicted": they depress recall (the classifier
    genuinely failed to find that class) but never precision (it made no claim). That
    is the standard treatment for a classifier with a reject option, and it is the one
    that matches what abstention costs here.
    """
    stats: dict[str, dict] = {}
    for category in REAL_CATEGORIES:
        tp = sum(1 for t, p in rows if p == category and t == category)
        fp = sum(1 for t, p in rows if p == category and t != category)
        fn = sum(1 for t, p in rows if t == category and p != category)
        precision = tp / (tp + fp) if tp + fp else float("nan")
        recall = tp / (tp + fn) if tp + fn else float("nan")
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision == precision and recall == recall and precision + recall
            else float("nan")
        )
        stats[category] = {"p": precision, "r": recall, "f1": f1, "support": tp + fn}
    return stats


def _fmt(value: float) -> str:
    return "  -  " if value != value else f"{value:.3f}"


def report(title: str, rows: list[dict], note: str = "") -> dict:
    """rows: [{"truth": str, "predicted": str, "id": str, "difficulty": str, ...}]"""
    print(f"\n{title}")
    print("=" * len(title))
    if note:
        print(note)
    if not rows:
        print("  (no rows)")
        return {}

    graded = [dict(r, outcome=grade(r["truth"], r["predicted"])) for r in rows]
    counts = Counter(r["outcome"] for r in graded)
    n = len(graded)

    pairs = [(r["truth"], r["predicted"]) for r in graded]
    stats = prf(pairs)

    print(f"\n  n = {n}")
    print(f"  {'category':<16}{'precision':>11}{'recall':>9}{'F1':>9}{'support':>9}")
    for category in REAL_CATEGORIES:
        s = stats[category]
        print(
            f"  {category:<16}{_fmt(s['p']):>11}{_fmt(s['r']):>9}{_fmt(s['f1']):>9}{s['support']:>9}"
        )

    macro = [stats[c]["f1"] for c in REAL_CATEGORIES if stats[c]["f1"] == stats[c]["f1"]]
    if macro:
        print(f"  {'macro F1':<16}{'':>11}{'':>9}{sum(macro) / len(macro):>9.3f}")

    print("\n  outcome breakdown")
    for key in (
        "correct",
        "correct_abstention",
        "safe_miss",
        "benign_error",
        "action_changing_error",
        "overconfident",
    ):
        if counts.get(key):
            print(f"    {key:<24}{counts[key]:>4}  ({counts[key] / n:.0%})")

    resolved = n - counts.get("safe_miss", 0) - counts.get("correct_abstention", 0)
    print(f"\n  coverage (a category was returned)   {resolved}/{n}  ({resolved / n:.0%})")

    dangerous = counts.get("action_changing_error", 0) + counts.get("overconfident", 0)
    print(f"  ACTION-CHANGING ERRORS               {dangerous}/{n}  ({dangerous / n:.0%})")
    if dangerous:
        print("    the rows where the system would have done the wrong thing:")
        for r in graded:
            if r["outcome"] in ("action_changing_error", "overconfident"):
                print(
                    f"      {r['id']}  {r.get('text', '')[:44]:<44} "
                    f"truth={r['truth']:<13} predicted={r['predicted']}"
                )

    confusion = defaultdict(int)
    for r in graded:
        confusion[(r["truth"], r["predicted"])] += 1
    labels = sorted({r["truth"] for r in graded} | {r["predicted"] for r in graded})
    if len(labels) > 1:
        print("\n  confusion (rows = truth, cols = predicted)")
        print("    " + " " * 18 + "".join(f"{lbl[:11]:>13}" for lbl in labels))
        for truth in labels:
            cells = "".join(f"{confusion[(truth, p)] or '.':>13}" for p in labels)
            print(f"    {truth:<18}{cells}")

    return {"n": n, "counts": dict(counts), "action_changing": dangerous}


def eval_rule_tier() -> dict:
    rows = load("error_codes.jsonl")
    out = []
    for row in rows:
        result = classify(Rail(row["rail"]), row["code"], "", source="failed_payment")
        out.append(
            {
                "id": row["id"],
                "truth": row["label"],
                "predicted": result.category.value,
                "difficulty": row["difficulty"],
                "in_table": row["in_table"],
                "text": row["code"],
                "source": result.source,
            }
        )

    defined = [r for r in out if r["in_table"]]
    novel = [r for r in out if not r["in_table"]]

    report(
        "Tier 1 - rule table, rows the table defines",
        defined,
        note=(
            "  These labels are read off the same dict the classifier consults, so a\n"
            "  perfect score here is definitional. Reported to prove the table is\n"
            "  internally consistent and reachable, NOT as evidence of accuracy."
        ),
    )

    summary = report(
        "Tier 1 - rule table, rows the table does NOT define",
        novel,
        note=(
            "  Realistic error codes absent from the taxonomy, plus case and\n"
            "  whitespace variants of codes that ARE in it. This is the informative\n"
            "  half: it measures what the table misses, and whether the misses are safe."
        ),
    )

    missed = [r for r in novel if r["predicted"] == "unknown"]
    unsafe = [r for r in missed if r["source"] != "rule_miss"]
    print(f"\n  every miss routed to human review: {'YES' if not unsafe else 'NO'} "
          f"({len(missed)} miss(es), all with source='rule_miss')")

    variants = [r for r in novel if r["difficulty"] == "adversarial" and r["truth"] != "unknown"]
    if variants:
        resolved = [r for r in variants if r["predicted"] != "unknown"]
        print(
            f"\n  case/format variants of in-table codes resolved: "
            f"{len(resolved)}/{len(variants)}"
        )
        for r in variants:
            mark = "resolved" if r["predicted"] != "unknown" else "MISSED  "
            print(f"      {mark}  {r['id']}  {r['text']!r}")
        if len(resolved) < len(variants):
            print(
                "    `taxonomy.lookup` is an exact dict match, so a mis-cased or\n"
                "    padded code from a PSP falls through to the LLM tier and, with no\n"
                "    credentials, to a human. Safe, but it spends a person on something\n"
                "    a `.strip().lower()` would resolve. Normalising the key is a real\n"
                "    fix; it is listed rather than silently patched here because this\n"
                "    harness exists to find exactly this."
            )
    return summary


def eval_llm_tier() -> dict:
    rows = load("narrations.jsonl")
    if not llm.available():
        print("\nTier 2 - LLM ensemble")
        print("=====================")
        print(
            "  SKIPPED: no ANTHROPIC_API_KEY.\n\n"
            "  This tier is deliberately NOT scored against the offline stub. The stub\n"
            "  returns scripted votes, so any precision it produced would be a measure\n"
            "  of the fixture, not of the classifier - a fabricated metric, which is\n"
            "  precisely what this project refuses to publish. Set a key to measure it,\n"
            "  or read the number as genuinely absent."
        )
        return {}

    from app import llm_classifier

    out = []
    for row in rows:
        result = llm_classifier.classify(Rail(row["rail"]), row["narration"])
        out.append(
            {
                "id": row["id"],
                "truth": row["label"],
                # An unaccepted ensemble IS an abstention: the caller receives UNKNOWN.
                "predicted": result.category.value if result.accepted else "unknown",
                "difficulty": row["difficulty"],
                "text": row["narration"],
                "reason": result.reason,
                "agreement": result.agreement,
            }
        )

    summary = report(
        "Tier 2 - LLM ensemble on free-text narration",
        out,
        note="  Live API. Three framings, majority vote, agreement and confidence floors.",
    )

    for difficulty in ("clean", "ambiguous", "adversarial"):
        subset = [r for r in out if r["difficulty"] == difficulty]
        if not subset:
            continue
        abstained = sum(1 for r in subset if r["predicted"] == "unknown")
        print(f"  abstention rate, {difficulty:<12} {abstained}/{len(subset)}")
    print(
        "\n  The ambiguous and adversarial rows SHOULD abstain. A high abstention rate\n"
        "  there is the ensemble working, not failing."
    )

    injection = next((r for r in out if r["id"] == "nr037"), None)
    if injection:
        obeyed = injection["predicted"] == "soft_decline"
        print(
            f"\n  prompt injection through the narration field (nr037): "
            f"{'OBEYED - this is a finding' if obeyed else 'not obeyed'}"
        )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm", action="store_true", help="also evaluate the LLM tier (needs credentials)")
    args = parser.parse_args()

    print("Classifier evaluation")
    print("---------------------")
    print(
        "The labelled set is HAND-AUTHORED against published error-code documentation\n"
        "and bank narration formats. No row came off a production webhook. These are\n"
        "not accuracy estimates for live Razorpay traffic. See eval/README.md."
    )

    rule = eval_rule_tier()
    llm_summary = eval_llm_tier() if args.llm else {}

    dangerous = rule.get("action_changing", 0) + llm_summary.get("action_changing", 0)
    print("\n" + "=" * 62)
    print(f"Action-changing errors across evaluated tiers: {dangerous}")
    print(
        "A misclassification can only move a payment between COMPLIANT actions -\n"
        "compliance is evaluated after classification and cannot be widened by it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
