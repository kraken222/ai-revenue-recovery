from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./recovery.db"

    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    afa_exempt_ceiling_paise: int = 1_500_000
    pre_debit_notice_hours: int = 24
    max_retry_attempts: int = 3
    retry_cooldown_hours: int = 24
    daily_contact_cap: int = 1
    control_group_rate: float = 0.15

    issuer_circuit_breaker_window_minutes: int = 60
    issuer_circuit_breaker_min_samples: int = 10
    issuer_circuit_breaker_decline_rate_threshold: float = 0.8

    # --- Bandit / economics (Sprint 2) ---
    bandit_enabled: bool = True
    max_retry_window_hours: int = 48
    # Cost of one customer contact attempt (SMS/WhatsApp/email + gateway overhead).
    contact_cost_paise: int = 200
    # Per-attempt hazard decay; stand-in for a fitted survival model, see economics.py.
    retry_hazard_decay: float = 0.7
    # Prior P(recovery) for hard-decline re-auth flows, where no bandit posterior exists.
    hard_decline_recovery_prior: float = 0.25
    # Probability one more dunning contact tips this customer into cancelling. Rises
    # linearly with attempts already made — this is what makes the EV gate actually bind.
    # With ltv_multiple below, this sets a breakeven recovery probability of
    # churn_risk_per_contact * ltv_multiple * (attempts + 1): ~9.6% on a first attempt,
    # rising to ~38% by the fourth. So a high-confidence slot earns more attempts than a
    # weak one, and the stopping point emerges from the economics instead of a hardcoded
    # cap. Tune against real churn cohorts before production.
    churn_risk_per_contact: float = 0.008
    # Customer lifetime value expressed as a multiple of one billing period's amount.
    ltv_multiple: float = 12.0

    # --- LLM layer (Sprint 3) ---
    # Number of differently-framed probes in the self-consistency ensemble. Capped by
    # the number of framings defined in llm_classifier; more probes cost linearly.
    llm_self_consistency_samples: int = 3
    # Share of usable probes that must agree before a classification is acted on.
    # 0.6 admits 2-of-3 (0.667) and 3-of-5, and rejects any even split, which the tie
    # check catches separately anyway. Deliberately NOT 0.67: two-thirds is
    # 0.6666..., so a 0.67 floor silently rejects the exact 2-of-3 majority this
    # threshold exists to allow — an off-by-epsilon that makes the stated policy and
    # the real one disagree.
    llm_min_agreement: float = 0.6
    # Mean self-reported confidence floor across the probes backing the winner.
    llm_min_confidence: float = 0.7
    # Draft customer messages with the LLM. Off by default: message copy leaves the
    # system and reaches a real person, so it is opt-in rather than opt-out.
    llm_copy_enabled: bool = False


settings = Settings()
