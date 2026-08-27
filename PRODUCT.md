# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Existing codebase: Python 3.12 / FastAPI / SQLAlchemy backend with SQLite (dev) or Postgres (prod). The dashboard is a single self-contained HTML page served by FastAPI from `app/static/index.html`, consuming the existing `/metrics/*` and `/payments/*` JSON endpoints. No build step, no framework, no external asset hosts — the page must run offline with zero external services, matching the rest of the project.

## Users

Two audiences on one surface, judges first (user-confirmed):

1. **Razorpay Buildathon judges** evaluating a Track 03 submission, watching a 5-minute pitch video and possibly cloning the repo. They are looking for problem taste, build quality, AI judgment, and honest failure handling. They need to see within seconds that the system works, is measured honestly, and never breaks a compliance rule.
2. **A merchant revenue-operations team** who would work this queue daily: triaging failing payments, understanding why the agent did what it did, and handling the exceptions it escalated.

## Product Purpose

An agent that detects recurring payments slipping away, decides the right intervention, and executes a bounded recovery workflow. Involuntary churn — payments failing for mechanical reasons rather than customer choice — is 20–40% of subscription churn. Success is money recovered that is provably attributable to the agent, with zero compliance breaches.

## Positioning

The mechanism a neighbouring product could not truthfully copy: **compliance is deterministic and hard-coded; only the optimisation inside the already-compliant action set is learned.** Each stage may narrow what the previous one permitted; none may widen it. The bandit cannot invent an action compliance forbade or extend a retry window; the EV gate can only ever downgrade to a stop. So every money action stays explainable and bounded even though part of the policy is learned.

Second differentiator: it is built for Indian payment rails. There is no card Account Updater under RBI tokenisation, and recurring debits run on UPI Autopay and eNACH mandates with their own AFA ceilings, pre-debit notice windows, and customer revocation rights. The system is rail-aware (Card / UPI Autopay / eNACH), not a port of a US card-centric retry engine.

## Operating Context

Razorpay webhooks arrive (`payment.failed`, `subscription.charged.failed`), are deduped on event id into an append-only event log, then flow through: classify → compliance → guardrails → bandit → EV gate → execute. Outcomes return as webhooks and close the learning loop. A worker executes decisions whose compliant slot is in the future. A 15% holdout receives no contact at all so recovery can be measured causally.

Judges will encounter this as a 5-minute screen recording plus a repo. Ops users would encounter it as a queue they work through, drilling into individual payments to understand agent reasoning.

## Capabilities and Constraints

- Rail-aware decline taxonomy across Card / UPI Autopay / eNACH, four categories (soft decline, hard decline, technical, risk block).
- Deterministic compliance core: AFA ceiling, pre-debit notice window, mandate revocation as absolute stop, uniform attempt cap.
- Operational guardrails: daily contact cap, per-issuer circuit breaker.
- Thompson Sampling bandit over `(rail, category, time-of-day bucket)` retry slots — 24 arms, each individually inspectable.
- Expected-value stopping rule including churn cost, which is what makes the gate actually bind.
- Full reasoning trace per payment: every classification, rule fired, guardrail check, bandit draw, EV computation, and action.
- Replicated ablation harness with paired confidence intervals.
- **Terminology that must be used exactly:** rail, decline category, compliant action set, holdout / control arm, causal lift, bandit arm, posterior, expected value, compliance invariant, audit trail.
- Three revenue-at-risk sources on three distinct compliance regimes: failed payment, abandoned checkout (not a debt — one nudge, no ladder), overdue MSMED receivable.
- Two-tier classification: rule table on clean error codes, self-consistency LLM ensemble across three *framings* on free-text bank narration. Every failure path resolves to `UNKNOWN` → `escalate_human`; inert without an API key.
- Signature-verified webhooks (HMAC-SHA256 over the raw body) and idempotent Razorpay Payment Links.
- **Not built:** `retry_charge` and `request_new_mandate` raise rather than call an unverified Razorpay API; off-policy evaluation is unimplemented; voice decides lawfulness and writes the script but does not dial; the LLM tier cannot be scored without credentials and the eval set is hand-authored rather than sampled from production, so the ensemble thresholds remain reasoned rather than fitted.

## Brand Commitments

Built for the Razorpay AI Buildathon. Razorpay is the platform being built on, not the author — the dashboard must not impersonate Razorpay branding or imply it is an official Razorpay product.

## Evidence on Hand

All figures come from a synthetic simulation, and this must never be presented as real Razorpay traffic:

- Causal recovery lift, intervention vs 15% holdout: **+46.3%** replicated across 12 seeds (a single `seed_synthetic_data` run varies roughly +45% to +55% by seed; quote the replicated figure, not one run's).
- Net economic value: gross recovered, contact spend, measured churn loss.
- Compliance invariants measured on executed actions, currently **6/6 passing**, asserted in CI against 300 payments driven through the full pipeline.
- Learned bandit posteriors that demonstrably recover a hidden time-of-day ground truth the model was never told.
- Replicated ablation, 400 payments × 12 seeds: full system vs fixed-schedule baseline, **mean net delta +₹5,482, 95% CI [−27,996, +38,959], 5/12 seeds improved — the CI includes zero, so the effect is not established at this n.**
- **204 passing tests.**
- Classifier precision/recall against a labelled set (`eval/`): precision 1.000 wherever the rule tier makes a claim, **0 action-changing errors**, 72% of undefined codes safely escalated. The set is hand-authored, not production traffic — it measures conformance to spec, never live accuracy.

**The flat ablation result is the honest one and must not be "corrected" upward.** An
earlier version of this file claimed +₹208,373 with a CI excluding zero and 11/12 seeds
improved. That number was real when written and is now wrong: two correctness fixes
erased it — modelling Razorpay's actual card-retry semantics (which removed the
over-contacting the EV gate existed to prevent) and making `ltv_multiple` a property of
the source rather than a global 12×. Both fixes are still right. Reverting to the
flattering figure would mean measuring the system against a baseline known to be wrong,
which the whole project exists to refuse. See [README § Does the complexity pay for
itself?](README.md#does-the-complexity-pay-for-itself).

Absences future work must not fabricate: no real merchant data, no real recovery rates, no production deployment, no customer testimonials, no Razorpay endorsement.

## Product Principles

1. **Compliance is never learned.** Regulatory and safety logic is deterministic and auditable; learning happens only inside the set it permits.
2. **A number without a counterfactual is not a result.** Every recovery claim is reported against a no-contact holdout.
3. **Measure the invariant, not its proxy.** Compliance is verified against actions actually executed, never inferred from a record's status.
4. **Name the limitation.** Synthetic data, unverified APIs, and uncalibrated assumptions are stated plainly; a system that hides these is worse than one that admits them.
5. **Not acting is a decision worth showing.** The system's biggest win comes from refusing negative-value chases, which raw recovery rate would score as a regression.

## Accessibility & Inclusion

Dense numeric interface. Status and pass/fail must never be encoded by colour alone — every state carries a text or glyph label, since red/green pass-fail is the most common colour-blindness failure in ops dashboards. Must remain legible when compressed into video.
