# Teleprompter — read this aloud, nothing else

Pure narration. Stage directions, prep and cut-order live in [PITCH.md](PITCH.md).

Timed at **~145 words per minute**, which is a natural pace for technical narration with
room to breathe. Word counts per section are measured, not estimated. Total 677 words
≈ **4:40**, leaving twenty seconds of headroom against the five-minute ceiling.

Two rules while reading:

1. **Pause at the paragraph breaks.** They are where the argument turns. Reading through
   them is how a 4:45 script becomes a 4:15 rush that lands nothing.
2. **Do not improvise numbers.** Every figure below is checked against the repo. If a
   number on screen differs from a number in your mouth, the judge trusts neither.

---

## 0:00 – 0:35 · The problem  *(80 words)*

A recurring payment fails for a mechanical reason. An expired card. Insufficient
balance. A mandate that lapsed. The customer still wants the service — nobody chose to
leave. The money leaves anyway.

That's involuntary churn, and it's twenty to forty percent of all subscription churn.

The obvious fix is the one Stripe published: learn the best time to retry, and retry
smarter.

In India, that design is wrong on arrival. Three reasons — and the third one changed
how I built this.

## 0:35 – 1:15 · Why India breaks it  *(98 words)*

First: there is no card Account Updater under RBI tokenisation. The thing that silently
repairs an expired card in the US doesn't exist here. So retry intelligence against a
dead instrument is optimising against a wall.

Second: recurring debit here runs on UPI Autopay and eNACH mandates, not just cards.
Each carries its own AFA ceiling, pre-debit notice window, and revocation rights.

Third — and this is the one: **Razorpay already retries cards itself.** Manual charge of
a domestic card isn't even supported. So a merchant-initiated card retry isn't a missing
feature. It's an action that should not exist.

## 1:15 – 2:05 · The architecture  *(113 words)*

One rule holds the whole system together.

**Compliance is deterministic and hard-coded. Only the optimisation inside the
already-compliant set is learned.**

Every stage may narrow what the previous stage permitted. None may widen it. The bandit
cannot invent an action compliance forbade, or stretch a retry window. The expected-value
gate has exactly one write it can make — downgrade to stop.

So every money action stays explainable and bounded, even though part of the policy is
learned.

Here's one payment going through it, live.

Classified soft decline — by rule, not by a model. Compliance returns a retry window
under COMP-006. The bandit draws a slot. The EV gate prices it. Scheduled, not yet due.

## 2:05 – 2:45 · Where the AI is, and isn't  *(96 words)*

Most declines arrive with a clean error code, and a dictionary lookup beats a model on
every axis — speed, cost, and accuracy. So the model doesn't touch them.

It handles only what the table can't: free-text bank narration, where every bank writes
its own wording.

And every failure path — split vote, tie, low confidence, API error, refusal, no
credentials — resolves to unknown, which compliance maps to human review.

The model can move a payment between compliant actions. It cannot invent one.

I measure that. Not raw accuracy — this classifier is allowed to refuse. Zero
action-changing errors.

## 2:45 – 3:50 · Measurement, and the result I didn't want  *(160 words)*

Two things make these numbers mean something.

A fifteen percent holdout gets no contact at all — so recovery is reported as a
difference, not a raw rate. And compliance is verified as invariants over actions
actually executed, never inferred from a record's status. Six of six pass, and CI fails
the build if any of them breaks.

Then I asked whether the learned machinery earns its keep. Four configurations, four
hundred payments, twelve seeds, paired.

**It doesn't.** Mean net delta about six thousand rupees. The confidence interval crosses
zero. Five of twelve seeds improved. Not established.

An earlier version showed a hundred and forty-eight thousand, with an interval that
cleanly excluded zero. Then I modelled Razorpay's real card-retry semantics, and contacts
per payment fell from three or four to a maximum of two.

Which means the EV gate's entire job had already been done — structurally — by getting
the compliance model right.

**Getting the domain right beat getting the algorithm right.**

## 3:50 – 4:25 · What breaks, and what happens  *(88 words)*

A 5xx from the gateway is recorded pending, never failed — calling it failed would let a
retry raise a second link against one debt.

Webhooks are HMAC-verified over the raw body. Before that, anyone with the URL could make
this system message a real customer.

An operator can hand a case back to the agent — and if the customer withdrew consent,
that's refused with a 409, written to the trail as a refusal.

If a button could authorise what the rules forbid, every compliance rule would be
advisory.

## 4:25 – 4:45 · Close  *(42 words)*

The world here is synthetic, and I never claim otherwise. No number in this repo is a
statement about real Razorpay traffic.

Compliance deterministic. Learning bounded inside it. Every rupee-moving action traceable
to the rule and the draw that produced it.

---

## The three lines that carry the pitch

If nerves compress everything else, these survive:

1. *"A merchant-initiated card retry isn't a missing feature. It's an action that should
   not exist."*
2. *"The model can move a payment between compliant actions. It cannot invent one."*
3. *"Getting the domain right beat getting the algorithm right."*

## Numbers, checked

Say these exactly. Anything not on this list, don't say.

| Claim | Value |
|---|---|
| Involuntary churn share | 20–40% of subscription churn |
| Causal recovery lift (single seeded run) | ~+50%, varies by seed |
| Compliance invariants | 6 of 6 |
| Ablation mean net delta | about ₹6,000 |
| Ablation CI | crosses zero |
| Seeds improved | 5 of 12 |
| Superseded earlier figure | ₹148,438 |
| Contacts per payment, after the card fix | 3–4 → max 2 |
| Action-changing classification errors | 0 |
| Tests | 226 |
