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


def lookup(rail: Rail, error_code: str) -> Category | None:
    return _TAXONOMY.get(rail, {}).get(error_code)


def error_codes_for_rail(rail: Rail) -> list[str]:
    return list(_TAXONOMY.get(rail, {}).keys())
