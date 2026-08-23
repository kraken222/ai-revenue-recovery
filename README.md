# AI Revenue Recovery

[![tests](https://github.com/kraken222/ai-revenue-recovery/actions/workflows/tests.yml/badge.svg)](https://github.com/kraken222/ai-revenue-recovery/actions/workflows/tests.yml)

An agent that detects payments slipping away, decides what to do about them, and executes a bounded recovery workflow — built for the Razorpay AI Buildathon, Track 03.

CI does more than run the tests: it drives 300 synthetic payments through the whole pipeline and **fails the build if any compliance invariant reports a breach**. A green tick means the loop ran and no contact rule was violated.

Failed recurring payments are not a niche problem: involuntary churn (a payment that fails for mechanical reasons, not because the customer chose to leave) is [20–40% of total subscription churn](https://link.springer.com/article/10.1057/s41270-025-00450-2). The customer wants to keep paying. The money leaves anyway.

---

## The one-line version

**Compliance is deterministic and hard-coded. Only the optimisation inside the compliant set is learned.**

Every stage may narrow what the previous one permitted; none may widen it.

```
classify      what kind of failure is this?          rules, per rail
compliance    what is LEGALLY allowed?               deterministic — never learned
guardrails    what is OPERATIONALLY wise?            deterministic — never learned
bandit        which allowed slot is best?            learned (Thompson Sampling)
EV gate       is one more attempt worth its cost?    learned posterior + economics
```

The bandit cannot invent an action compliance forbade, cannot extend a retry window, and cannot override a stop. The EV gate can only ever *downgrade* to `stop_lost`. So every money action stays explainable and bounded even though part of the policy is learned — which is the property the whole design exists to preserve.

---

## Why the obvious design is wrong in India

The reflex is to copy [Stripe Smart Retries](https://stripe.com/blog/how-we-built-it-smart-retries) or [Butter](https://www.butterpayments.com/resources/blog/how-to-use-machine-learning-to-recover-failed-recurring-payments). Both are card-centric and US-shaped. Two Indian regulatory facts break that playbook outright:

**1. There is no card Account Updater in India.** Visa/Mastercard Lifecycle Management — the thing that silently repairs an expired card in the US — [is not supported under RBI's tokenisation rules](https://www.chargebee.com/docs/payments/2.0/others/rbi-tokenization-regulations). A genuinely expired card cannot be retried into success. The correct action is re-registration, not a smarter retry schedule. A system that applies retry-timing intelligence to a hard decline here is optimising against a wall.

**2. Recurring debits run on mandates, not just cards.** UPI Autopay and eNACH dominate, and they carry their own rules: an AFA-exemption ceiling for debits without per-transaction 2FA, a pre-debit notification window, and customer-revocable mandates that must stop collection immediately.

**3. Razorpay already retries cards, and manual card charge is not supported.** Its own
dunning re-attempts a failed subscription charge daily until the subscription halts, and
[manual charge of a domestic card is explicitly unsupported](https://razorpay.com/docs/subscriptions/payment-retries/).
So a merchant-initiated card retry is not an unfinished feature — it is an action that
**should not exist**. Compliance routes cards to `monitor_gateway_retry` (a genuine
no-op that spends no attempt and logs no contact) and merchant recovery begins only at
the handover, when the gateway gives up and the subscription halts. This system's card-rail
job starts exactly where Razorpay's ends.

**4. Customer contact is confined to 08:00–19:00, but a silent debit is not contact.**
RBI's Fair Practices Code restricts recovery *communication* to those hours; outside
them it is classified as harassment. A machine-to-machine debit against an existing
mandate reaches nobody and is unrestricted — which matters, because the bandit correctly
learns that **00:00–06:00 recovers the most money** (salary credits land overnight). The
money-optimal slot and the legal contact window genuinely disagree, and only actions that
touch a person are bound by the second. [`app/contact_policy.py`](app/contact_policy.py)
makes that split, in IST rather than UTC — 19:00 IST is 13:30 UTC, so a naive `dt.hour`
check would permit contact four and a half hours past the legal close.

So the system is built around a **rail-aware taxonomy** (Card / UPI Autopay / eNACH), each with its own error-code map and its own legal action set — not a single generic "retry" action. See [`app/taxonomy.py`](app/taxonomy.py) and [`app/compliance.py`](app/compliance.py).

> The specific thresholds in `.env.example` are configured approximations. Reconcile them against current RBI circulars and your Razorpay account's live error codes before this touches real money.

---

## What is actually learned

### Retry timing — Thompson Sampling ([`app/bandit.py`](app/bandit.py))

Fixed schedules (24h / 48h / 72h) assume success probability is uniform across time. It isn't — it clusters around salary credits and wallet top-ups. Adyen published this exact problem shape in [AutoRescue](https://www.adyen.com/knowledge-hub/rescuing-failed-subscription-payments-using-contextual-multi-armed-bandits): a contextual bandit whose action space is future retry times within the allowed window, reward 1 on a successful charge. Stripe independently reports strong time-of-day effects.

Arms are keyed `(rail, category, time-of-day bucket)` — 24 arms, small enough to converge on a few-hundred-payment batch, and each arm's win rate is readable straight off the row. Exploration comes free from posterior width; no epsilon schedule needed.

**It works.** The simulator hides a time-of-day multiplier the bandit is never told, and the learned posteriors recover its ordering from binary outcomes alone:

```
Ground truth (hidden):   00:00 → 1.45   06:00 → 1.05   18:00 → 0.85   12:00 → 0.70

Learned mean posterior:  00:00 → 66.2%  06:00 → 63.4%  18:00 → 47.1%  12:00 → 45.6%
```

Each arm reports a **Wilson score interval**, not just a mean. Two arms can share a win rate at 3 pulls and 30 while warranting completely different confidence, and a bandit surface that hides that is showing a point estimate under the word "posterior". Wilson specifically rather than a normal approximation, because these arms live at low n where the normal approximation is not merely coarse but wrong in the flattering direction — on Beta(5,3) it puts the ceiling at 94% against an exact ~89%.

### Stopping — expected value, not a magic number ([`app/economics.py`](app/economics.py))

"Stop after 3 attempts" is arbitrary. The real question at each decision point is whether one more attempt is worth what it costs:

```
EV = P(recovery) × amount          upside
   − contact_cost                   the SMS/gateway spend
   − P(churn) × LTV                 the part everyone forgets
```

**The third term is the one that matters, and omitting it produces a fake stopping rule.** With only a contact cost, the gate never binds — a ₹2 SMS against even a 1% shot at ₹499 clears the bar trivially, so "EV-based stopping" silently degenerates into "always retry" while the attempt cap does all the real work. I shipped that bug first; a test caught it.

The genuine cost of dunning is churn. Every retry is a prompt for the customer to reconsider the subscription, and a customer lost to dunning fatigue costs their whole remaining lifetime value, not one invoice. Modelling it makes the rule bind in the right place, and it produces behaviour a fixed cap cannot express:

- A high-confidence retry slot **earns more attempts** than a weak one.
- A ₹5 invoice **stops being chased** at odds a ₹9,999 one clears easily.
- The stopping point **emerges from the economics** rather than being declared.

`P(recovery)` combines the bandit's posterior with a per-attempt hazard decay. That decay is an explicit stand-in for a proper survival model — recovery is a time-to-event problem and the principled version is a Cox fit on `(attempts, time-since-failure, category)`. The decay captures the direction honestly without pretending to rigour that needs real traffic to earn.

### Classification — rules first, model only where they run out ([`app/llm_classifier.py`](app/llm_classifier.py))

Most failures arrive with a clean Razorpay `error_code`, and the rule table resolves
those exactly. Reaching for a model there would be slower, costlier and *less* accurate
than a dict lookup. The model handles only the residue: free-text narration forwarded
from a bank or PSP, which no rule table covers exhaustively because every bank writes
its own wording.

**The self-consistency trick had to be adapted, and the reason is specific.** Textbook
self-consistency samples one prompt N times at temperature > 0 and takes the majority.
That is unavailable here — `temperature`, `top_p` and `top_k` are removed on Claude
Opus 5 and return a 400. Repeating one identical prompt would mostly re-measure the
same computation and manufacture false agreement: three identical answers that were
never three independent opinions.

So the ensemble varies the **framing** instead of the sampling — direct classification,
operational consequence, and exclusion — and takes the majority across framings.
Agreement then means the answer is a property of the evidence rather than of one
prompt's phrasing, which is the failure mode that actually bites.

```
python -m scripts.demo_llm_classifier      # runs offline with a scripted stub
```

```
[ACCEPTED] upi_autopay  INSUFFICIENT BAL IN AC XX4471 AS ON DATE
             direct       soft_decline   conf 0.95
             consequence  soft_decline   conf 0.95
             exclusion    soft_decline   conf 0.95
             => soft_decline  agreement 100%  (accepted)

[-> HUMAN] card         DO NOT HONOUR
             direct       soft_decline   conf 0.55
             consequence  risk_block     conf 0.5
             exclusion    technical      conf 0.4
             => unknown  agreement 33%  (tied_vote)
```

`DO NOT HONOUR` is the case worth looking at: a real decline string that legitimately
spans funds, risk and issuer policy. The right answer is to escalate, not to guess, and
the split vote produces exactly that.

Every failure path — split vote, tie, low confidence, API error, safety refusal, no
credentials — resolves to UNKNOWN, which compliance maps to `escalate_human` under
COMP-002. **The model can move a payment between compliant actions; it cannot invent
one.** With no API key the whole tier is inert and the system behaves exactly as it did
before it existed.

One detail that was a real bug: an even split is not a majority. With one probe
erroring, two usable votes can each hold 50%, and `Counter.most_common` would break the
tie by insertion order — silently turning a coin flip into a confident decision. That
is now an explicit rejection with its own test.

### Customer copy — templates ship, the model only improves them ([`app/messaging.py`](app/messaging.py))

Generation is **off by default**, because this is the only stage whose output reaches a
real person. Every message has a deterministic template that is correct and sendable
with no API call; a generated draft has to pass validation to replace one, and a draft
that invents a rupee figure, exceeds the channel budget, or reaches for a threat is
discarded for the template. A payment reminder is never blocked on an inference call.

SMS and WhatsApp are template-only by design, not by omission: commercial SMS in India
runs over DLT-registered templates, so free-form copy there would not be legally
sendable. Email is the only channel the model is allowed to rewrite.

### Escalation and promises to pay ([`app/escalation.py`](app/escalation.py))

The bar names "compliant escalation", and a flat action set does not satisfy it. The
ladder escalates **channel, specificity and human involvement** — never pressure, because
RBI's code prohibits threats and coercion at every rung:

```
rung 0  passive    the gateway is still retrying; say nothing
rung 1  reminder   one soft message, no call to action
rung 2  assisted   payment link / re-auth with an explicit next step
rung 3  human      operator review with the full trace
```

Rung 3 is a person, not a harder machine. Two paths reach it — two missed promises, or
two contacts already made — and the audit trail names which, because reporting both as
"repeated broken promises" would put a reason in the record that never happened. That
was a real bug: the escalation entry hardcoded the promise reason, and the two cases it
fired on had no promises at all.

A **promise to pay** is a dated commitment stored as a record rather than a note, so it
can suppress outreach until it matures and be counted when it breaks. Chasing someone
who already said "Friday" is how you lose a customer who intended to pay. Three details
that turned out to matter:

- **Superseded is not broken.** A revised date is not a missed one. Filing revisions as
  breaks means a customer who rescheduled twice gets escalated for keeping us informed.
- **A grace period prevents false breaks.** Payments settle overnight, so treating the
  promised evening as an instant deadline manufactures broken promises out of ordinary
  settlement lag.
- **A promise can only ever suppress.** It never creates a contact, never shortens a
  compliance window, and never reopens a case a revoked mandate already stopped.

### Three sources, three regimes ([`app/sources.py`](app/sources.py))

Track 03 names all three kinds of slipping revenue in one sentence — "payment failures
and checkout abandonment to overdue receivables" — and they genuinely are one problem:
detect, decide, act, bounded. They are emphatically **not one compliance regime**, and
collapsing them is what makes a recovery agent creepy or illegal.

| | debt owed | mandate held | contact budget | ladder | retry possible |
|---|---|---|---|---|---|
| failed payment | yes | yes | 3 | full | yes (mandate rails) |
| abandoned checkout | **no** | no | **1** | **none** | **no** |
| overdue invoice | yes | no | 4 | full | **no** |

**An abandoned checkout is not a debt.** Nobody owes anything — the customer looked and
left. Running dunning escalation against them would be wrong on its own terms, and under
TCCCPR it is a marketing contact dressed as a service one. So that source gets exactly
one nudge and no ladder. The dunning framing would also simply be false: saying "your
payment failed" about a checkout nobody ever submitted is a untrue statement, not a
tone problem.

**Neither of the new sources can be retried at all.** No mandate exists, so a retry is
not discouraged there — it does not exist as an action. That is now an enforced
invariant (`retry attempted without a mandate: 0`), asserted on executed actions rather
than on what compliance intended.

### Overdue B2B receivables — the law is already chasing ([`app/receivables.py`](app/receivables.py))

Chasing a late Indian B2B invoice inverts the usual leverage, because the MSMED Act
does the work:

- **s.15** — payment is due on the agreed date **or 45 days, whichever is earlier**. A
  contract saying "90 days net" does not move the appointed day.
- **s.16** — from that day the buyer owes **compound interest, monthly rests, at three
  times the RBI bank rate** (~18.75%). It accrues by operation of law.
- **s.43B(h)** (from April 2024) — the buyer also **loses the tax deduction** while it
  remains unpaid.

So the most effective thing a receivables agent can do is not apply pressure but
**state what is already accruing.** That is information, and the distinction from a
threat is not cosmetic: RBI prohibits coercion, and reciting a statutory consequence
that operates whether or not anyone mentions it is not coercion.

Two details the implementation gets right because getting them wrong would put a false
number in a letter to a buyer: interest uses **monthly rests, not simple interest**
(the Act says compound), and **partial months do not rest** — a month that has not
completed has not compounded, and counting it overstates a statutory figure.

### Hinglish voice — the most gated action in the system ([`app/voice.py`](app/voice.py))

This module **decides whether a call would be lawful and writes what would be said. It
does not dial.** TCCCPR is specific enough that a fake `place_call` returning success
would make every check below decorative:

- **Number series is mandatory** — 1600 for transactional, 140 for promotional.
- **Any upsell reclassifies a collections call as Promotional**, so the script must
  carry no offer at all.
- **DND/NCPR must be checked** before dialling; penalties run ₹2–10 lakh.
- **An automated call must disclose that it is automated within 15 seconds** — which is
  why the script is a list of timed segments rather than a paragraph. A prose blob
  cannot be verified against a positional rule.

The verifier runs against the script, not the generator, so a translated or shortened
variant clears the same bar as the handwritten original.

```
+ 0s  [disclosure] Namaste. Yeh Acme Foods ki taraf se ek automated call hai.
+ 6s  [identify  ] Aapka Rs.999 ka payment complete nahi ho paya.
+12s  [reason    ] Aapka UPI AutoPay mandate process nahi hua.
```

```
python -m scripts.demo_sources      # all three regimes side by side, offline
```

---

## Does the complexity pay for itself?

[`scripts/ablation.py`](scripts/ablation.py) runs the same batch under four configurations to answer this with numbers rather than assertion.

Getting this measurement *right* took three corrections, each of which had made the result look better than it was:

1. **Each config was scored with its own cost parameters** — so an EV-disabled run valued churn at zero and appeared free. Fixed: scoring uses constant reference costs. A policy is never graded by its own rulebook.
2. **Control-group composition drifted between configs** (20.0% / 18.2% / 16.4%) because assignment came from a live RNG draw, so the stream diverged once policies differed. Fixed: holdout assignment is a hash of the payment id — deterministic, order-independent, stable across variants, which is how production experiment frameworks bucket units and for exactly this reason.
3. **The evaluation counted a churn benefit the world never implemented.** Contacting a customer had no downside in the simulation, so the EV gate could only ever give up revenue while being credited for savings that never occurred. Fixed: churn is now a simulated event, and the ablation *measures* lost customers instead of inferring them.

Even then, churn is a rare, high-cost event, so net value from any single run is mostly noise. Results are replicated across seeds with common random numbers and paired per seed, which removes between-world variance — the dominant term.

```
python -m scripts.ablation 400 12
```

400 payments × 12 seeds, across all three sources:

| config | recovery | lift vs control | contacts | churned | net ₹ (mean ± sd) |
|---|---|---|---|---|---|
| fixed-schedule | 88.1% | +49.8% | 150 | 2.8 | 2,199,444 ± 441,194 |
| +bandit | 88.2% | +49.9% | 143 | 2.5 | 2,198,749 ± 445,288 |
| +ev-gate | 75.3% | +37.0% | 121 | 1.8 | 2,202,464 ± 428,131 |
| **full** | 84.6% | +46.3% | 138 | 2.3 | **2,205,009 ± 425,806** |

```
full vs fixed-schedule, paired by seed:
  mean net delta  Rs.+5,565  (sd 58,009, se 16,746)
  95% CI          Rs.-27,927 .. Rs.+39,057
  seeds improved  5/12
  CI includes zero - effect not established at this n
```

**The learned machinery does not pay for itself on this workload, and that is the most
interesting result in the project.**

An earlier version showed the full system beating the baseline by ₹148,438 with a
confidence interval that cleanly excluded zero. Then I checked Razorpay's actual retry
semantics and found the system had been wrong about the card rail: the gateway runs its
own dunning, and merchant-initiated card retries were duplicating attempts the network
was already making. Fixing that dropped contacts per payment from 3–4 to a maximum of 2.

Which means the EV gate's entire job — refusing the third and fourth chase — had already
been done, structurally, by getting the compliance model right. There was almost nothing
left for it to prevent.

Getting here honestly took two further corrections, and both had been hiding a bad
result:

**The significance test could only detect improvement.** It read `lo > 0`, so an
interval lying entirely *below* zero — a statistically significant harm — was reported
as "effect not established". A test that only recognises good news is not a test; it is
a filter that flatters whatever it measures. It now reports both directions, and the
first thing it reported was that a configuration was significantly worse.

**The EV gate was charging subscription churn against B2B invoices.** `ltv_multiple` was
one global constant at 12×, which is right for a subscription — losing it forfeits every
future period — and badly wrong for a one-off invoice, where a buyer settling late is not
cancelling anything. The gate was therefore refusing to chase the single most valuable
recoverable items in the batch, and the ablation measured that as a real −₹74,886 harm
with a CI excluding zero. LTV is now a property of the source (12× subscription, 2×
invoice, 0× abandoned checkout), which moved the result to the flat +₹5,565 above.

The honest reading:

- **Two correctness fixes each erased a headline number, and both fixes are still right.**
  Reverting them would restore "improvement" that existed only because the baseline was
  over-contacting, or because the evaluator was charging a cost the world does not.
- **Getting the domain right beat getting the algorithm right.** The bandit and EV gate
  are sound, and on a workload with genuine over-contacting they demonstrably help. On a
  correctly-modelled one they are close to redundant.
- **`+ev-gate` still cuts churned customers by a third** (2.8 → 1.8) and still trades
  ~13pp of recovery rate to do it. The tradeoff is real; at this contact volume it nets
  out flat.

I am reporting the flat result rather than the flattering one because the alternative is
a system that measures its own value against a baseline it knows to be wrong. Recovery
rate and net value still **trade off by design** — judge on net.

---

## Running it

```bash
python -m venv .venv && source .venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env
```

Everything runs with zero external services: SQLite by default, and a `DryRunGateway` stands in whenever Razorpay credentials are absent, so the full pipeline is demoable offline.

```bash
python -m pytest tests/ -q                    # 143 tests, all offline
python -m scripts.seed_synthetic_data 300     # full pipeline + causal lift + learned arms
python -m scripts.ablation 400 12             # does the learned machinery earn its keep?
python -m scripts.demo_llm_classifier         # self-consistency ensemble, offline stub
python -m scripts.demo_sources                # three revenue-at-risk regimes, offline
uvicorn app.main:app --reload                 # dashboard at http://localhost:8000
```

`ANTHROPIC_API_KEY` is optional. Unset, the LLM tier is inert: the rule table still
classifies every clean error code, free-text narrations route to human review rather
than being guessed at, and message templates ship as written. Nothing else changes.

## The dashboard

`GET /` serves an operations dashboard built as an auditor's working papers — every figure traced to source, exceptions listed rather than buried, and compliance asserted against executed actions rather than inferred from status.

It is a single self-contained HTML page with no build step and no external hosts, consistent with the offline constraint. Schedules A-1 through A-5 carry the lead figures, the full register (all 300 payments, no pagination), the compliance assertions, the learned posteriors with their intervals, and the schedule of exceptions. Selecting any payment opens its complete reasoning trace — including the bandit's actual Thompson draws per arm and the reward that closed the loop:

```json
{ "arm_key": "upi_autopay|technical|18",
  "thompson_samples": { "0": 0.0968, "6": 0.1887, "12": 0.5322, "18": 0.9444 },
  "tod_bucket": 18, "scheduled_at": "2026-01-02T18:00:00+00:00" }
{ "arm_key": "upi_autopay|technical|18", "reward": 1 }
```

That is what "every money action explainable" means concretely: not a confidence score, but the draw that chose the slot and the outcome that updated it.

---

## Measuring honestly

**Recovery rate alone is not a result.** A share of failed payments self-cure with no intervention at all, so a raw "we recovered 60%" claim attributes that to the system for free. A 15% holdout receives no contact, and the reported figure is the **difference** between arms.

The holdout withholds the *intervention only*. A compliance hard-stop is the absence of an intervention, not one, so it lands identically in both arms — otherwise the arms end in different terminal states and the comparison is biased. (This was a real bug: the control override initially masked `stop_lost`, parking revoked-mandate payments where the self-cure path could resurrect them.)

Compliance is verified as **invariants over executed actions**, not inferred from status:

```
[PASS] contacted after mandate revocation: 0
[PASS] control-group payments contacted: 0
[PASS] payments exceeding attempt cap: 0
[PASS] risk-blocked payments auto-actioned: 0
[PASS] abandoned checkouts contacted more than once: 0
[PASS] retry attempted without a mandate: 0
```

Checking a payment's *status* is not a compliance check. Only whether a contact was actually executed against the gateway tells you whether a rule was breached — a distinction that caught a false positive in my own metric.

The revocation invariant then needed a second correction, and it is the more interesting one. "Did we contact after revocation?" was implemented as "does a revoked payment have any contacts?" — a different question, and a wrong one, because a customer can revoke *after* a contact that was entirely legal when it was made. Counting those retroactively made the invariant unpassable for something that was not a violation. It now compares each contact's timestamp against `mandate_revoked_at`. Six tests pin the semantics, including the case that was silently failing.

---

## Production concerns that are actually built

- **Idempotent, event-sourced ingestion.** Razorpay redelivers webhooks; every event is deduped on its id and the append-only log is the source of truth, so derived state can be rebuilt by replay.
- **Per-issuer circuit breaker.** If one issuer's decline rate spikes over a rolling window (a bank-side outage, not individually bad payments), retries against it back off rather than hammering a system that is already down.
- **Scheduled execution.** A future-dated compliant slot is genuinely deferred to a worker, not executed immediately — an early version computed the pre-debit notice window correctly and then called the gateway anyway, silently violating the window it had just calculated.
- **Full reasoning trace.** Every classification, rule fired, guardrail check, bandit draw, EV computation and action is queryable per payment: `GET /payments/{id}/audit`.

---

## Known limitations

Stated plainly, because a system that hides these is worse than one that names them.

- **The world is synthetic.** Ground-truth recovery and churn probabilities are invented. The bandit demonstrably learns the hidden structure and the ablation is internally valid, but no number here is a claim about real Razorpay traffic.
- **`retry_charge` is not wired to a verified API.** Only Payment Links is a confirmed Razorpay call. Razorpay runs its own dunning on subscriptions, so forcing a retry may not be the right primitive at all — this needs reconciling against live docs before go-live, and the code raises rather than pretending otherwise.
- **`churn_risk_per_contact` is assumed, not calibrated.** It sets the breakeven recovery probability, so the EV gate is only as good as this number. It needs real cohort data.
- **The LLM classifier has never been evaluated against labelled data.** The ensemble, the thresholds and the fall-through are built and tested, but "is 2-of-3 agreement the right bar?" is an empirical question and there is no labelled set of real bank narrations here to answer it. The thresholds are reasoned, not fitted.
- **No off-policy evaluation before promoting a policy.** [Adyen's approach](https://arxiv.org/html/2501.10470v1) is the right reference. Today a bandit update takes effect immediately.
- **Voice dialling is not implemented.** The TRAI compliance gate and the Hinglish script are built and tested; the carrier integration, the DLT-registered 1600-series originator and the live NCPR lookup are not.
- **The RBI bank rate is a single constant.** Interest spanning a rate change should use each rate for its own window; a real implementation needs a dated rate table.
- **Time-of-day buckets are UTC**, not customer-local — which matters for exactly the salary-cycle effect being exploited.
- **The dashboard's display face is a system mono stack.** At 58px the letterform is doing design work, and `ui-monospace` resolves to a different face on every OS. A self-hosted typewriter face would serve the world better; shipping the system stack was a deliberate call to keep the page dependency-free, not an oversight.

---

## Research grounding

| Idea | Source |
|---|---|
| Retry timing as a contextual bandit; reward = successful charge | [Adyen AutoRescue](https://www.adyen.com/knowledge-hub/rescuing-failed-subscription-payments-using-contextual-multi-armed-bandits) |
| Off-policy evaluation before promoting a payments policy | [Adyen, arXiv 2501.10470](https://arxiv.org/html/2501.10470v1) |
| Time-of-day effects in retry success; ML over decline signals | [Stripe Smart Retries](https://stripe.com/blog/how-we-built-it-smart-retries) |
| Involuntary churn as 20–40% of subscription churn; survival framing | [Journal of Marketing Analytics](https://link.springer.com/article/10.1057/s41270-025-00450-2) |
| Decline codes each need their own action, not one retry rule | [Butter Payments](https://www.butterpayments.com/resources/blog/how-to-use-machine-learning-to-recover-failed-recurring-payments) |
| Gate anything financially material or hard to reverse | [FinHarness, arXiv 2605.27333](https://arxiv.org/html/2605.27333v1) |
| Reason/act loop for the classification sub-agent | [ReAct, arXiv 2210.03629](https://arxiv.org/pdf/2210.03629) |
| No card Account Updater / LCM under RBI tokenisation | [Chargebee](https://www.chargebee.com/docs/payments/2.0/others/rbi-tokenization-regulations) · [GoDaddy](https://www.godaddy.com/help/faq-network-tokens-and-lifecycle-management-lcm-updates-41961) |
| E-mandate AFA ceiling and pre-debit notice | [Zoho](https://zoho.com/billing/academy/payment-collection-and-compliance/faqs-RBIs-new-auto-debit-rules.html) |
