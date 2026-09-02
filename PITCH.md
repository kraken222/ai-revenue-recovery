# 5-minute pitch — shot script

Track 03 submission. Target 4:45, hard ceiling 5:00.

**The spine of this pitch is the flat result.** The bandit and EV gate do *not* beat a
plain compliant retry loop on this workload, and the reason they don't is that getting
the Indian compliance model right had already removed the over-contacting they existed
to prevent. That is the most interesting thing the project found. If it lands as a
footnote in minute four it reads as a null result and a weak submission; if it is the
argument, it reads as someone who measured their own work honestly and can tell the
difference between a win and a measurement artefact. Build toward it from 0:00.

Do not open by listing features. Open with the constraint that makes the problem hard.

---

## Before recording

Have these open as tabs / windows, in this order, so no shot needs a "let me just…":

1. Terminal, cleared, in the repo root, venv active.
2. `GET /console` — freshly loaded, feed empty.
3. `GET /` — dashboard, already seeded with 300 payments.
4. `ARCHITECTURE.md` on GitHub (diagrams rendered — check they render *before* you hit record).
5. The ablation output, already run and scrolled to the paired-CI block.

Pre-run so nothing is waiting on a process during the take. The `rm` matters — a stale
database carries test rows from earlier work into the register, on camera:

```bash
rm recovery.db && python -m scripts.seed_synthetic_data 300
```

```bash
python -m scripts.ablation 400 12 > ablation.txt
```

Start the server in its own terminal and leave it running:

```bash
uvicorn app.main:app --reload
```

Word-for-word narration is in [PITCH_NARRATION.md](PITCH_NARRATION.md) — 677 words,
4:40 at a natural pace. Read that; use this file for what to put on screen.

Record at 1920×1080. Terminal font large enough to read at half-scale — judges may watch
in a small window. Dashboard is dense; zoom to 125% before recording.

---

## 0:00 – 0:35 · The problem, and the constraint that makes it hard

**Screen:** README top, then cut to the four-rail argument.

> A recurring payment fails for a mechanical reason — expired card, insufficient
> balance, a mandate that lapsed. The customer still wants the service. The money leaves
> anyway. That's involuntary churn, and it's twenty to forty percent of all subscription
> churn.
>
> The obvious fix is Stripe Smart Retries: learn the best time to retry. In India that
> design is wrong on arrival, for three reasons.

Do not rush this. The next 30 seconds are the whole differentiator.

## 0:35 – 1:15 · Why India breaks it

**Screen:** README § "Why the obvious design is wrong in India".

> One: there is no card Account Updater under RBI tokenisation, so an expired card
> cannot be silently repaired. Retry intelligence against a dead instrument is
> optimising against a wall.
>
> Two: recurring debit here runs on UPI Autopay and eNACH mandates, not just cards —
> each with an AFA ceiling, a pre-debit notice window, and revocation rights.
>
> Three, and this one changed the whole design: **Razorpay already retries cards
> itself**, and manual charge of a domestic card isn't supported. So a merchant-initiated
> card retry isn't a missing feature — it's an action that should not exist. My card-rail
> job starts exactly where Razorpay's dunning ends.

## 1:15 – 2:05 · The architecture, in one sentence and one screen

**Screen:** `ARCHITECTURE.md` decision-funnel diagram. Let it sit — do not scroll while talking.

> One rule holds the whole system together. **Compliance is deterministic and
> hard-coded. Only the optimisation inside the already-compliant set is learned.**
>
> Every stage may narrow what the previous stage permitted. None may widen it. The
> bandit cannot invent an action compliance forbade or stretch a retry window. The EV
> gate has exactly one write it can make — downgrade to stop. So every money action stays
> explainable and bounded even though part of the policy is learned.

**Cut to the terminal, then `/console`.** One command, and the chain prints:

```bash
python -m scripts.demo_webhook
```

The payment id is pinned to one that hashes into the *treated* arm. That matters: a
randomly-chosen id lands in the holdout about fifteen percent of the time, and a control
payment's chain correctly ends in `control_no_action` — which is right, and a dead
demo. Fired live it reads:

```
ingestion       upi_autopay insufficient_balance
classification  soft_decline (rule)
compliance      COMP-006-compliant-retry-window -> retry_at
guardrail       retry_at
escalation      rung 1 reminder
bandit          slot 18:00 - posterior 44%
economics       EV Rs.346 - p=44.4% - positive_expected_value
decision        retry_at
execution       scheduled, not yet due
```

**Do not promise a specific slot.** The bandit is Thompson Sampling — it *draws*, so the
slot and posterior change every run. Say "the bandit draws a slot, and that draw is in
the audit trail" and read whatever came up. If it draws a low-posterior slot, that is
exploration working, and saying so is stronger than pretending it always picks the best
one.

The "it learned midnight is best" claim belongs to the **aggregate posteriors on the
dashboard**, not to one live draw. Keep those two moments separate — conflating them is
the one place this demo could overclaim.

## 2:05 – 2:45 · Where AI is, and where it deliberately isn't

**Screen:** README § classification, then the eval output.

> Most declines arrive with a clean error code, and a dict lookup beats a model there on
> every axis. The model only handles what the table can't: free-text bank narration,
> where every bank writes its own wording.
>
> And it's an ensemble across three *framings*, not three samples — temperature is gone
> on Opus 5, so re-running one prompt would manufacture agreement rather than measure it.
>
> Every failure path — split vote, tie, low confidence, API error, refusal, no
> credentials — resolves to UNKNOWN, which compliance maps to human review. The model can
> move a payment between compliant actions. It cannot invent one.

**Screen:** `python -m scripts.eval_classifier` output.

> And I measure it on the thing that matters. Not raw accuracy — this classifier is
> allowed to refuse, so accuracy would score a safe escalation and a confident wrong
> answer identically. I grade errors by whether they'd have *changed what the system did*.
> Zero action-changing errors. The eval also found a real bug: lookup was exact-match, so
> a mis-cased code from a PSP was quietly spending a human.

## 2:45 – 3:50 · Measurement, and the result I didn't want

**Screen:** dashboard `/`, compliance invariants panel.

> Two things make these numbers mean something. A fifteen percent holdout gets no contact
> at all, so recovery is reported as a difference, not a raw rate. And compliance is
> verified as invariants over **actions actually executed** — not inferred from a record's
> status. Six of six pass, and CI fails the build if any of them breaks.

**Cut to ablation output.** Slow down here.

> Then I asked whether the learned machinery earns its keep. Four configurations, 400
> payments, twelve seeds, paired.
>
> **It doesn't.** Mean net delta about six thousand rupees, confidence interval crosses zero,
> five of twelve seeds improved. Not established.
>
> An earlier version of this showed a hundred and forty-eight thousand with a clean
> interval. Then I modelled Razorpay's actual card-retry semantics and contacts per
> payment dropped from three or four to a maximum of two — which means the EV gate's entire job, refusing the
> third and fourth chase, had already been done structurally by getting the compliance
> model right. There was almost nothing left for it to prevent.
>
> I'm reporting the flat number because the alternative is measuring my system against a
> baseline I know to be wrong. **Getting the domain right beat getting the algorithm
> right** — and that's a result, not a failure.

## 3:50 – 4:25 · Failure handling

**Screen:** README § Razorpay boundary / known limitations.

> Things that break, and what happens when they do. A 5xx from the gateway is recorded
> pending, never failed — calling it failed would let a retry raise a second payment link
> against one debt. Webhooks are HMAC-verified over the raw body; before that, anyone with
> the URL could POST a fabricated failure and make this system message a real customer.
>
> An operator can hand a case back to the agent — and if the customer withdrew consent,
> that's refused with a 409, and the refusal is written to the audit trail. If a button
> could authorise what the rules forbid, every rule in the compliance module would be
> advisory.
>
> And I separate "verified" from "built". The mandate-rail charge is a real call now —
> order, then create-recurring, the documented endpoint — but I've never run it against
> a live mandate, because that needs a token from a real customer authorisation. So the
> tests pin the request shape and the README says the round trip is outstanding.
> `request_new_mandate` is a multi-step registration flow, not one call, so it still
> raises rather than pretending.

## 4:25 – 4:45 · Close

**Screen:** README limitations, then repo root.

> The world here is synthetic and I never claim otherwise — no number in this repo is a
> statement about real Razorpay traffic. The ablation is exactly reproducible, the
> compliance gate runs in CI, and everything runs offline on a fresh clone with no
> credentials.
>
> Compliance deterministic. Learning bounded inside it. Every rupee-moving action
> traceable to the rule and the draw that produced it.

---

## Things to cut first if you run long

In this order — each is the least load-bearing at its point:

1. The eNACH/MSMED receivables detail (§2:05) — strong, but the card-rail argument
   already carries the "rail-aware" claim.
2. The ensemble-framings explanation — keep "three framings not three samples", drop the
   temperature reasoning.
3. The 5xx/pending detail in failure handling — keep the 409 refusal, it's stronger.

## Do not cut

- The three-reason India argument. It is the differentiator.
- The flat ablation result and *why* it went flat.
- The 409 refusal of a human override.
- The sentence stating the data is synthetic.
