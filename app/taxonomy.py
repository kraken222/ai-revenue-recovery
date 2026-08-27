"""Rail-aware decline taxonomy.

Maps a (rail, error_code) pair to a recovery category. The exact string values below
are a reasonable approximation of Razorpay's published error codes per payment method
(https://razorpay.com/docs/errors/) — reconcile against the live docs for your account
before this touches real money. What matters structurally, and what should survive
that reconciliation unchanged, is the four-way split itself:

- SOFT_DECLINE : transient, customer-side (e.g. insufficient balance) -> safe to retry
                 later within compliant limits.
- HARD_DECLINE : instrument itself is dead (expired card, cancelled mandate) -> retrying
                 the same instrument is pointless; the correct action is asking the
                 customer to re-register (India has no card network Account Updater /
                 Lifecycle Management under RBI's tokenization rules, so this can't be
                 silently fixed like it can on US card rails).
- TECHNICAL    : gateway/issuer/NPCI infrastructure hiccup -> retry, and feed the
                 issuer/rail into the circuit breaker.
- RISK_BLOCK   : fraud/risk engine declined it -> never auto-retried, human only.
"""

from app.models import Category, Rail

_TAXONOMY: dict[Rail, dict[str, Category]] = {
    Rail.CARD: {
        "insufficient_funds": Category.SOFT_DECLINE,
        "card_declined": Category.SOFT_DECLINE,
        "limit_exceeded": Category.SOFT_DECLINE,
        "expired_card": Category.HARD_DECLINE,
        "invalid_card": Category.HARD_DECLINE,
        "card_not_activated_for_international": Category.HARD_DECLINE,
        "authentication_failed": Category.HARD_DECLINE,
        "issuer_unavailable": Category.TECHNICAL,
        "gateway_timeout": Category.TECHNICAL,
        "processing_error": Category.TECHNICAL,
        "fraud_suspected": Category.RISK_BLOCK,
        "risk_check_failed": Category.RISK_BLOCK,
    },
    Rail.UPI_AUTOPAY: {
        "insufficient_balance": Category.SOFT_DECLINE,
        "debit_declined_by_bank": Category.SOFT_DECLINE,
        "mandate_not_active": Category.HARD_DECLINE,
        "mandate_revoked": Category.HARD_DECLINE,
        "mandate_limit_exceeded": Category.HARD_DECLINE,
        "psp_app_unavailable": Category.TECHNICAL,
        "npci_timeout": Category.TECHNICAL,
        "bank_server_down": Category.TECHNICAL,
        "debit_declined_by_bank_risk": Category.RISK_BLOCK,
    },
    Rail.ENACH: {
        "insufficient_funds": Category.SOFT_DECLINE,
        "mandate_cancelled": Category.HARD_DECLINE,
        "account_closed": Category.HARD_DECLINE,
        "invalid_account": Category.HARD_DECLINE,
        "bank_technical_failure": Category.TECHNICAL,
        "npci_enach_timeout": Category.TECHNICAL,
        "suspected_fraud": Category.RISK_BLOCK,
    },
}

# Actions that make sense per category, independent of compliance/guardrail filtering.
# The compliance layer narrows this further; it never widens it.
CATEGORY_ALLOWED_ACTIONS: dict[Category, list[str]] = {
    Category.SOFT_DECLINE: ["retry_now", "retry_at"],
    Category.TECHNICAL: ["retry_now", "retry_at"],
    Category.HARD_DECLINE: ["send_payment_link", "request_new_mandate"],
    Category.RISK_BLOCK: ["escalate_human"],
    Category.UNKNOWN: ["escalate_human"],
}


def normalise_code(error_code: str) -> str:
    """Fold the formatting variance a real PSP feed carries, before lookup.

    This was an exact dict match, and `scripts/eval_classifier.py` caught what that
    costs: `EXPIRED_CARD`, `Insufficient_Funds`, ` npci_timeout ` and
    `insufficient-funds` all missed the table and fell through to human review. The
    failure was *safe* — a miss becomes UNKNOWN and escalates, never a guess — but it
    spends an operator on a case a fold would have resolved, and a PSP that starts
    upper-casing its codes would silently move most of its traffic to the review queue
    without a single test going red.

    Deliberately conservative: case, surrounding whitespace, and the hyphen/underscore
    and space/underscore splits. It does not stem, fuzzy-match or edit-distance, because
    a near-miss between two codes that genuinely differ is exactly the case that must
    reach a human rather than be coerced into the closest-looking rule.
    """
    return "_".join(error_code.strip().lower().replace("-", " ").replace("_", " ").split())


_NORMALISED: dict[Rail, dict[str, Category]] = {
    rail: {normalise_code(code): category for code, category in codes.items()}
    for rail, codes in _TAXONOMY.items()
}


def lookup(rail: Rail, error_code: str) -> Category | None:
    return _NORMALISED.get(rail, {}).get(normalise_code(error_code))


def error_codes_for_rail(rail: Rail) -> list[str]:
    return list(_TAXONOMY.get(rail, {}).keys())
