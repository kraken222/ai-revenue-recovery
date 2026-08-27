"""Tests for the classification eval harness and the code normalisation it forced.

Two things are pinned here. The first is `taxonomy.normalise_code`, which exists
because the eval found that a mis-cased or padded error code missed the table and
spent a human. The second is the harness's own grading, because a metric that scores
itself wrong is worse than no metric — it would let the project publish a number it
had not actually earned.
"""

import json
from pathlib import Path

import pytest

from app.classifier import classify
from app.models import Category, Rail
from app.taxonomy import lookup, normalise_code
from scripts.eval_classifier import EVAL_DIR, action_set, grade, load, prf

# --- normalisation -------------------------------------------------------------


@pytest.mark.parametrize(
    "rail,code",
    [
        (Rail.CARD, "EXPIRED_CARD"),
        (Rail.CARD, "Expired_Card"),
        (Rail.CARD, "  expired_card  "),
        (Rail.CARD, "expired-card"),
        (Rail.CARD, "expired card"),
    ],
)
def test_formatting_variants_resolve_to_the_same_category(rail, code):
    assert lookup(rail, code) is Category.HARD_DECLINE


def test_normalisation_is_rail_scoped_not_global():
    # `insufficient_funds` is a card/eNACH code; UPI Autopay spells it
    # `insufficient_balance`. Folding format must not fold rails.
    assert lookup(Rail.CARD, "INSUFFICIENT-FUNDS") is Category.SOFT_DECLINE
    assert lookup(Rail.UPI_AUTOPAY, "INSUFFICIENT-FUNDS") is None


def test_normalisation_does_not_fuzzy_match():
    """A near-miss must still miss.

    The fold is formatting-only on purpose. Coercing an unrecognised code onto the
    closest-looking rule is precisely the guess this classifier refuses to make.
    """
    assert lookup(Rail.CARD, "expired_cards") is None
    assert lookup(Rail.CARD, "card_expired") is None
    assert lookup(Rail.CARD, "expired") is None


def test_normalisation_leaves_genuine_misses_escalating():
    result = classify(Rail.CARD, "  SOME_BRAND_NEW_PSP_CODE  ")
    assert result.category is Category.UNKNOWN
    assert result.source == "rule_miss"


def test_empty_code_does_not_match_anything():
    assert lookup(Rail.CARD, "") is None
    assert lookup(Rail.CARD, "   ") is None


def test_normalise_code_is_idempotent():
    for raw in ("EXPIRED_CARD", " npci_timeout ", "insufficient-funds", "Do Not Honour"):
        once = normalise_code(raw)
        assert normalise_code(once) == once


# --- the harness's own grading -------------------------------------------------


def test_abstention_on_a_labelled_row_is_a_safe_miss_not_an_error():
    assert grade("soft_decline", "unknown") == "safe_miss"


def test_abstention_on_an_unlabelled_row_is_correct():
    """`unknown` as a *label* means no confident answer is defensible, so escalating
    is the right outcome — scoring it as a miss would penalise the behaviour the
    ensemble exists to produce."""
    assert grade("unknown", "unknown") == "correct_abstention"


def test_answering_an_unanswerable_row_is_overconfidence():
    assert grade("unknown", "soft_decline") == "overconfident"


def test_soft_technical_confusion_is_benign_because_the_action_is_identical():
    assert action_set("soft_decline") == action_set("technical")
    assert grade("soft_decline", "technical") == "benign_error"
    assert grade("technical", "soft_decline") == "benign_error"


def test_soft_hard_confusion_is_action_changing():
    assert action_set("soft_decline") != action_set("hard_decline")
    assert grade("soft_decline", "hard_decline") == "action_changing_error"


def test_risk_block_confusion_is_action_changing():
    assert grade("risk_block", "soft_decline") == "action_changing_error"


def test_abstention_depresses_recall_but_never_precision():
    """The whole point of a reject option: refusing to answer is not a false positive.

    One correct call and one abstention on the same class must read as perfect
    precision and half recall. If abstention leaked into precision, a classifier
    would be punished for escalating and rewarded for guessing.
    """
    stats = prf([("soft_decline", "soft_decline"), ("soft_decline", "unknown")])
    assert stats["soft_decline"]["p"] == 1.0
    assert stats["soft_decline"]["r"] == 0.5
    assert stats["soft_decline"]["support"] == 2


def test_a_wrong_confident_answer_does_hurt_precision():
    stats = prf([("hard_decline", "soft_decline")])
    assert stats["soft_decline"]["p"] == 0.0


def test_precision_is_nan_when_a_class_was_never_predicted():
    """Not zero. A class that was never claimed has undefined precision, and
    reporting 0.0 would drag a macro average down for a claim never made."""
    stats = prf([("soft_decline", "unknown")])
    assert stats["soft_decline"]["p"] != stats["soft_decline"]["p"]  # NaN


# --- the dataset itself --------------------------------------------------------

VALID_LABELS = {"soft_decline", "hard_decline", "technical", "risk_block", "unknown"}
VALID_DIFFICULTY = {"clean", "ambiguous", "adversarial"}


@pytest.mark.parametrize("filename", ["error_codes.jsonl", "narrations.jsonl"])
def test_eval_dataset_is_well_formed(filename):
    rows = load(filename)
    assert rows, f"{filename} is empty"
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate ids"
    for row in rows:
        assert row["label"] in VALID_LABELS, f"{row['id']}: bad label {row['label']}"
        assert row["difficulty"] in VALID_DIFFICULTY, f"{row['id']}: bad difficulty"
        assert row["rail"] in {r.value for r in Rail}, f"{row['id']}: bad rail"
        assert row.get("note"), f"{row['id']}: every row must say why it exists"


def test_in_table_flags_are_accurate():
    """The `in_table` flag drives which half of the report is treated as definitional.
    If it drifts from the taxonomy, the harness silently mislabels its own evidence."""
    for row in load("error_codes.jsonl"):
        resolved_exactly = lookup(Rail(row["rail"]), row["code"]) is not None
        if row["in_table"]:
            assert resolved_exactly, f"{row['id']} claims in_table but the table misses it"


def test_eval_dataset_files_are_ascii():
    """Console output is read on a Windows terminal in this project; a smart quote in
    a narration renders as a replacement character and corrupts the row it labels."""
    for filename in ("error_codes.jsonl", "narrations.jsonl"):
        text = (EVAL_DIR / filename).read_text(encoding="utf-8")
        non_ascii = {c for c in text if ord(c) > 127}
        assert not non_ascii, f"{filename} contains non-ascii: {non_ascii}"


def test_every_error_code_row_is_json_per_line():
    path = Path(EVAL_DIR / "error_codes.jsonl")
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            json.loads(line)  # raises with the line number in context if malformed
