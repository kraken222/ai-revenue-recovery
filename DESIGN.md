---
name: Recovery Working Papers
description: An auditor's grained green-bar working paper for a payment-recovery agent — every figure traced, every exception listed.
colors:
  paper: "#F6F5EE"
  bar: "#D6E4CD"
  bar-edge: "#BACFAF"
  rule: "#9FB295"
  rule-faint: "#C8CCBE"
  desk: "#E7E5DA"
  ink: "#1B1C18"
  ink-soft: "#5B5E52"
  ink-faint: "#5F6257"
  red: "#B3271E"
  graphite: "#4A4E45"
  indigo: "#26467F"
  violet: "#57337E"
  amber: "#8A5B12"
typography:
  display:
    fontFamily: "ui-monospace, Cascadia Mono, SF Mono, Menlo, Consolas, monospace"
    fontSize: "clamp(32px, 5.6vw, 58px)"
    fontWeight: 700
    lineHeight: 1.02
    letterSpacing: "-0.035em"
  headline:
    fontFamily: "ui-monospace, Cascadia Mono, SF Mono, Menlo, Consolas, monospace"
    fontSize: "clamp(26px, 3.9vw, 40px)"
    fontWeight: 700
    lineHeight: 1.02
    letterSpacing: "-0.035em"
  title:
    fontFamily: "ui-monospace, Cascadia Mono, SF Mono, Menlo, Consolas, monospace"
    fontSize: "clamp(17px, 2.3vw, 25px)"
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "0.10em"
  subtitle:
    fontFamily: "ui-monospace, Cascadia Mono, SF Mono, Menlo, Consolas, monospace"
    fontSize: "13px"
    fontWeight: 700
    lineHeight: 1.5
    letterSpacing: "0.13em"
  body:
    fontFamily: "ui-monospace, Cascadia Mono, SF Mono, Menlo, Consolas, monospace"
    fontSize: "13.5px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  cell:
    fontFamily: "ui-monospace, Cascadia Mono, SF Mono, Menlo, Consolas, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  label:
    fontFamily: "ui-monospace, Cascadia Mono, SF Mono, Menlo, Consolas, monospace"
    fontSize: "11px"
    fontWeight: 700
    lineHeight: 1.5
    letterSpacing: "0.11em"
rounded:
  none: "0px"
spacing:
  hair: "2px"
  xs: "5px"
  sm: "8px"
  md: "12px"
  lg: "20px"
  gutter: "clamp(16px, 2.6vw, 34px)"
  block: "clamp(18px, 2.6vw, 30px)"
components:
  schedule-ref:
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "1px 7px"
  stamp:
    backgroundColor: "transparent"
    textColor: "{colors.violet}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 6px"
  stamp-lg:
    backgroundColor: "transparent"
    textColor: "{colors.violet}"
    rounded: "{rounded.none}"
    padding: "3px 12px"
  stamp-fail:
    textColor: "{colors.red}"
  stamp-waiting:
    textColor: "{colors.amber}"
  stamp-executed:
    textColor: "{colors.indigo}"
  stamp-neutral:
    textColor: "{colors.ink-soft}"
  xref:
    backgroundColor: "transparent"
    textColor: "{colors.indigo}"
    rounded: "{rounded.none}"
    padding: "0px"
  xref-hover:
    backgroundColor: "#DCE3F0"
    textColor: "{colors.indigo}"
  button-close:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "2px 9px"
  button-close-hover:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
  open-trace:
    backgroundColor: "transparent"
    textColor: "{colors.indigo}"
    rounded: "{rounded.none}"
    padding: "0 5px"
  table-head-cell:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink-soft}"
    typography: "{typography.label}"
    padding: "7px clamp(8px, 1vw, 12px)"
  table-cell:
    textColor: "{colors.ink}"
    typography: "{typography.cell}"
    padding: "5px clamp(8px, 1vw, 12px)"
  table-row-bar:
    backgroundColor: "{colors.bar}"
    textColor: "{colors.ink}"
  table-row-hover:
    backgroundColor: "#F0E4C4"
    textColor: "{colors.ink}"
  table-row-open:
    backgroundColor: "#EBDCB2"
    textColor: "{colors.ink}"
  sheet:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "2px 0 8px"
  support-panel:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "0 18px 24px"
    width: "min(560px, 100%)"
---

# Design System: Recovery Working Papers

## Overview

**Creative North Star: "The Auditor's Working Paper"**

This is a working paper, not a dashboard. The surface behaves like grained green-bar
columnar analysis stock sitting on a desk: one continuous sheet, printed column rules
rather than UI borders, an index block in the top-right corner, schedules addressed
A-1 through A-5 that genuinely cross-reference each other, and marks struck in the
margin recording work that was done. Every headline figure carries a tick and a
traced-to reference; every stop the agent made is listed on its own schedule rather
than absorbed into an aggregate.

It refuses the SaaS analytics look in specific, checkable ways: no elevated metric
cards, no sparkline filler, no accent-hued hero number, no chip-shaped pills, no
rounded corners anywhere. Density is high and deliberate — the register runs all 300
rows in one uncapped scroll because seeing the whole register at once is the argument.
The only elevation in the system is the sheet's own drop shadow, because paper on a
desk casts one.

The world is painted, not inherited. Light is explicit (`color-scheme: light`) and
there is no dark mode: a paper world has one look, and shipping a second would make
the material a theme instead of a material. Everything is self-hosted — type is a
system mono stack, the grain is an inline SVG `feTurbulence` data-URI, there are no
webfonts, CDNs or external images. That is a hard product constraint (the project must
run offline with zero external services), not a stylistic preference.

**Key Characteristics:**
- Grained green-bar stock as the literal material, with alternating printed bars
- Monospace throughout, tabular figures, one monumental dominant number per screen
- Zero corner radius; rules and stamps instead of cards and chips
- Status colour as law — exactly one hue per meaning
- No state encoded by colour alone; every state carries a word or a drawn mark
- Single-look, offline-first: no dark mode, no external assets

## Colors

A printed palette pulled from real analysis stock: warm off-white paper, a green bar,
tinted greenish inks, and four small status hues that each mean one thing.

### Primary
- **Ledger Paper** (`{colors.paper}`): the sheet itself, and the background of sticky
  table heads so column heads stay opaque as rows pass under them.
- **Green Bar** (`{colors.bar}`): every odd table row. This is the historical reason
  ledgers stripe at all; it is stock, not zebra decoration.
- **Bar Edge** (`{colors.bar-edge}`): the printed top and bottom edge of a bar row.

### Secondary
- **Exception Red** (`{colors.red}`): exceptions, fail, lost, human-review, negative
  figures. Nothing else, ever.
- **Pencil Graphite** (`{colors.graphite}`): tick marks and footings — the drawn
  check, caret and circle that record work done.
- **Cross-Reference Indigo** (`{colors.indigo}`): traced-to links, rule ids, posterior
  interval strokes, the focus ring, and the executed status.
- **Assertion Violet** (`{colors.violet}`): the assertion stamp and pass. Records
  assertions that actually ran, never a human review that did not.
- **Waiting Amber** (`{colors.amber}`): pending / waiting only.

### Neutral
- **Working Ink** (`{colors.ink}`): all primary text, the 2px header rule, the 1.5px
  thead underline, and the index block's outer border.
- **Soft Ink** (`{colors.ink-soft}`): labels, secondary meta, dim table cells.
  6.07:1 on paper, 5.00:1 on the bar.
- **Faint Ink** (`{colors.ink-faint}`): footer, schedule notes, table subheads.
  5.70:1 on paper, 4.70:1 on the bar — the worst contrast anywhere in the build.
- **Printed Rule** (`{colors.rule}`): scrollbar thumb, posterior midpoint tick.
- **Faint Rule** (`{colors.rule-faint}`): interior column rules, dotted separators,
  schedule dividers.
- **Desk** (`{colors.desk}`): the surface beneath the sheet, warmed by a radial
  gradient from `#EFEDE2` to `#D8D6C9`.

### Named Rules

**The One Hue Per Meaning Rule.** Each status colour carries exactly one meaning and
appears nowhere else. Red is exception / fail / lost. Graphite is tick marks and
footings. Indigo is cross-reference. Violet is the assertion stamp. Amber is waiting.
Do not reach for a status hue because it looks right; reach for it because the
meaning matches. Audit test: if you can find red on anything that is not a failure,
the law is broken.

**The Graphite Pencil Rule.** Positive marks are struck in graphite, not red. The
auditor's pencil hierarchy is real — exceptions are red, but ticks and footings are
graphite. A red check beside "6/6 assertions clean" reads as a failure and undoes the
line it sits on. This was a live finding: the positive ticks had been painted in the
failure hue and were corrected to graphite.

**The Worst-Ground Rule.** Both secondary ink tiers were contrast-solved against the
worst ground they land on — the green bar (`{colors.bar}`), not the paper. Any new
secondary ink must clear 4.5:1 on the bar, and its measured ratios belong in a comment
beside the token. Inks are tinted from the stock's own hue rather than grayed, so they
read as printed ink instead of faded UI text.

**The Label-And-Colour Rule.** No state may be encoded by colour alone. Every stamp
carries its word ("pass", "fail", "waiting"); every tick carries an SVG `<title>`.
The posterior column carries evidence strength as the *width of a real Wilson score
interval*, not as opacity — an opacity ramp meaning "low evidence" was removed for
exactly this reason.

## Typography

**Display Font:** system monospace stack (`ui-monospace`, Cascadia Mono, SF Mono,
Menlo, Consolas)
**Body Font:** the same stack — one face for the entire surface
**Label Font:** the same stack at 11px, uppercase, wide-tracked

**Character:** A single mono voice, treated as a typewriter rather than a code editor:
monumental at the top of the lead schedule, tight and tabular in the register, and
wide-tracked uppercase for anything a reader scans rather than reads. `tabular-nums`
is set globally so figures align down a column without effort.

**Decided constraint, with its tradeoff named:** the display face is the system mono
stack rather than a self-hosted typewriter face. At 58px the letterform *is* the
design, and a self-hosted face would serve this world better and render identically
across machines; the system stack means the display line looks different on macOS,
Windows and Linux. Shipping the system stack is a deliberate user decision made
under the zero-external-assets constraint, not an unexamined default. If a self-hosted
face is ever added it must be bundled locally — no font CDN.

### Hierarchy
- **Display** (700, `clamp(32px, 5.6vw, 58px)`, 1.02, -0.035em): the single dominant
  figure in the lead schedule's wide first bay. One per screen.
- **Headline** (700, `clamp(26px, 3.9vw, 40px)`, 1.02, -0.035em): the two supporting
  lead figures, a deliberate step below the dominant.
- **Title** (700, `clamp(17px, 2.3vw, 25px)`, 1.15, +0.10em, uppercase): the page's
  working-paper title.
- **Subtitle** (700, 13px, +0.13em, uppercase): schedule names (A-1, A-2 …) and the
  supporting-panel heading at 12.5px.
- **Body** (400, 13.5px, 1.5): running prose, capped at 62–74ch.
- **Cell** (400, 13px, 1.5, nowrap): every table cell.
- **Label** (700, 11px, +0.11em, uppercase): column heads, index block, trace stage
  names, stamps, footer.

### Named Rules

**The 11px Floor Rule.** 11px is the hard floor and nothing functional sits below it.
The surface is read in compressed screen recordings as well as on a desk. The 11–11.5px
tier (column heads, stamps, index block, trace stages, footer) is the deliberate
scanned-label tier — text you locate, not text you read. Body stays at 13.5px and
table cells at 13px.

**The Readable-Label Exception.** The three lead-schedule labels sit at 12.5px, not
11px. They are the labels a judge must read in the first seconds of a compressed
recording, so they do not get the scanned-label size.

**The Dominant Figure Rule.** The lead schedule has a dominant, never a tie. Three
co-equal figures at one size is the stat-row rhythm this world refuses. One figure
takes the display size and the wide bay (1.25fr); the others step down.

## Layout

One centered sheet, `max-width: 1440px`, floated on a desk with `clamp(12px, 2.5vw, 36px)`
of body padding. Inside the sheet the page is a vertical stack of `.schedule` sections
separated by faint printed rules, each opening with a bordered A-n reference chip, an
uppercase schedule name, and a right-aligned note.

Horizontal rhythm is a single gutter token, `clamp(16px, 2.6vw, 34px)`, applied by
every schedule's own children — head, lead grid, assertions list, first and last table
cells. Vertical rhythm runs on 5 / 8 / 12 / 20px steps.

The lead schedule is a three-bay grid at `1.25fr 1fr 1fr`, bays divided by a 1px
printed rule, collapsing to a single column below 720px where the vertical dividers
become horizontal top rules.

**The Bleeding Rule Rule.** `.sheet` carries no horizontal padding. The `border-bottom`
on `.wp-head` and `.schedule` therefore runs edge to edge across the paper, which is
the ruled-stationery grammar — a rule on a sheet spans the sheet. Content is inset by
its own children instead (measured 20.8px at default width). Automated design checks
flag this as `cramped-padding`; it is a knowing exception and must not be "fixed" by
adding padding to `.sheet`, which would inset every rule and destroy the grammar.

**The Page-Is-The-Scrollport Rule.** `.scroller` is `overflow-x: visible` above 1100px
and `auto` at or below it. Any non-`visible` overflow value makes the element a
scrollport, which would break the page-level sticky `thead` (it would stick to a
9000px box nobody scrolls) and trap the wheel mid-page — fatal in the primary scene,
a screen recording. The 1100px threshold is derived, not chosen: nine nowrap columns
at 13px mono measure ~980px, the sheet loses ~72px to body padding, plus headroom for
a longer decline code. **Do not round it to 1024px or 1200px.** Wide registers carry
`min-width: 720px`; narrow tables (A-5's three columns) deliberately do not, so they
never scroll sideways on mobile for no reason.

Breakpoints: 1100px (scroller) and 720px (header block, lead grid, and trace rows all
collapse to single column).

## Elevation & Depth

Almost entirely flat. Depth comes from the material — grain, printed bars, printed
rules — not from shadows. Exactly two elements cast: the sheet, and the supporting
schedule that slides over it. Nothing else is lifted, and there are no hover
elevations.

### Shadow Vocabulary
- **Sheet on desk** (`box-shadow: 0 1px 1px rgba(27,28,24,.16), 0 2px 5px -1px rgba(27,28,24,.14), 0 14px 30px -14px rgba(27,28,24,.30)`):
  the paper's own edge. Offset and blurred like real contact shadow.
- **Pulled sheet** (`box-shadow: -18px 0 40px -18px rgba(27,28,24,.35)`): the drill-down
  panel, cast leftward onto the sheet it was pulled from.

### Named Rules

**The Edge-Is-The-Shadow Rule.** Paper has no stroked outline. The sheet commits to
elevation alone with no hairline border, which is both physically truer and avoids the
hairline-plus-diffuse-shadow tell of generic UI cards. (The drill-down panel is the one
exception: it needs a 1px ink border on its left edge because that edge is a cut, not
a paper edge.)

**The Grain-Rides-With-The-Background Rule.** The grain data-URI must travel in the
same `background` shorthand as any colour it sits on, with `background-blend-mode:
multiply`. Declaring `background-image` separately loses it — a later shorthand resets
it to `none`, which once produced grained paper with grainless stripes: the exact
opposite of one continuous sheet.

## Shapes

**Zero radius everywhere.** No element in the build has a `border-radius` other than
0 — including the scrollbar thumb, which is explicitly reset. Printed matter has square
corners.

The form vocabulary is rules and boxes: 1px faint rules for interior divisions, 1px ink
for the index block and reference chips, 1.5px ink under column heads, 2px ink under
the working-paper header and the drill-down head, and 1px dotted faint rules inside
lists (assertions, trace entries). Stamps are hollow boxes outlined in `currentColor`
at 1.5–2px, never filled. The large assertion stamp is rotated -2.5° about its left
edge — the only rotation in the system, and the reason it reads as a stamp rather
than a badge.

Marks (tick, caret, circle) are drawn as inline 13px SVG paths with round caps at
1.6–1.7px stroke, never Unicode glyphs and never an icon font.

## Components

### Schedule Reference (A-n)
- **Character:** an address, not an ornament. A-1…A-5 are real cross-reference targets.
- **Shape:** 1px ink box, square, `1px 7px` padding, 11.5px, 700, +0.10em, uppercase.
- **Rule:** never invent a reference that nothing points at, and never let two things
  share one address (the file's own ref is RCV-01 precisely because A-1 is taken).

### Cross-Reference Link (`.xref`)
- **Character:** a leader line carrying a value, rendered as a real `<button>`.
- **Style:** indigo text, 1px underline in `currentColor`, no background, inherits font.
- **Hover:** pale indigo wash (`#DCE3F0`). No colour change, no motion.
- **Focus:** the global 2px indigo `:focus-visible` outline at 2px offset.

### Status Stamp
- **Character:** rubber-stamped, hollow, uppercase, 11px, +0.10em.
- **Shape:** 1.5px `currentColor` box, zero radius, `0 6px` padding. Never filled — the
  colour lives in the text and border together.
- **States:** violet = pass/recovered, red = fail/lost/human-review, amber = waiting,
  indigo = executed, soft ink = new/classified/decided.
- **Large variant:** 12.5px, 2px border, `3px 12px`, rotated -2.5°, used once, in the
  header block beside the index the reviewer would initial.

### Green-Bar Table
- **Character:** the register; the working surface of the whole page.
- **Head:** sticky to the viewport, paper background, label typography, 1.5px ink
  underline, nowrap. Optional `.th-sub` line names the estimator in 400 weight.
- **Rows:** odd rows carry the green bar plus grain, with `bar-edge` printed top and
  bottom. Even rows are bare paper. Cells are 13px, nowrap, 5px vertical padding.
- **States:** clickable rows go warm ochre on hover (`#F0E4C4`, edges `#DCC98F`) and
  hold a deeper ochre when open (`#EBDCB2`, edges `#C9B173`).
- **Semantics:** activation lives on a real `<button>` in the last cell. Never put
  `role="button"` on a `<tr>` — it strips the table semantics screen readers navigate by.

### Posterior Interval
- **Character:** the signature component. Evidence strength is shown as geometry.
- **Track:** 11px tall, `#E4E2D6`, 1px faint-rule border, with a dotted midpoint tick
  at 50% because the axis is an absolute 0–100%, not max-normalised.
- **Interval:** `#B9C6DC` fill bounded by 1px indigo strokes at each end; the mean is a
  2px indigo rule across it. A wide span reads as thin evidence with no legend and no
  opacity trick.

### Supporting Schedule (drill-down)
- **Character:** a sheet pulled from the stack.
- **Shape:** fixed right panel, `min(560px, 100%)`, paper ground, 1px ink left border.
- **Motion:** `pull` — 420ms `cubic-bezier(.16,1,.3,1)`, 14px inward translate plus
  fade, from `display:none` so nothing animates on first paint. Disabled under
  `prefers-reduced-motion`.
- **Trace rows:** `88px 1fr` grid, dotted faint separators, uppercase 11px stage name
  with a faint actor line beneath; collapses to one column below 720px.

### Buttons
- **Close:** 1px ink box, transparent, 11.5px +0.08em, `2px 9px`. Hover inverts to ink
  ground with paper text. This is the only inversion in the system.
- **Open trace:** indigo text on a transparent 1px transparent border that becomes
  indigo on hover; bolds when its row is open. Reads as a worked mark, not button chrome.

### Empty, Loading and Error States
- **Loading:** stepped skeleton bars, 11px tall, sweeping 1.3s linear gradient, disabled
  under `prefers-reduced-motion`.
- **Empty / error:** a `.msg` block with an uppercase 11.5px lead line and an inline
  `code` chip (`#E7E5D8`, 1px faint-rule border) carrying the actual command to run.
  Error lead lines are the only red in these blocks.

## Do's and Don'ts

### Do:
- **Do** paint light explicitly (`color-scheme: light`, an opaque `html` background).
  This world has one look.
- **Do** keep every new colour inside the One Hue Per Meaning law, and contrast-check
  new inks against the green bar (`{colors.bar}`), not the paper.
- **Do** pair every colour-carried state with a word or a drawn mark.
- **Do** draw marks and icons as inline SVG paths with round caps.
- **Do** let schedule rules bleed edge to edge and inset content with the gutter token
  `clamp(16px, 2.6vw, 34px)` on the children.
- **Do** keep the register uncapped and unpaginated; the whole register at once is the
  argument.
- **Do** ship every asset locally — system font stacks, inline SVG data-URIs, no CDN.
- **Do** give any new animation a `prefers-reduced-motion: reduce` escape.

### Don't:
- **Don't** add `border-radius` to anything. Zero is the system.
- **Don't** add horizontal padding to `.sheet`, or set `.scroller` to any overflow
  other than `visible` above 1100px, or round the 1100px threshold. All three are
  load-bearing.
- **Don't** use red for anything that is not an exception, failure or loss — especially
  not for a positive tick.
- **Don't** encode any state with opacity, saturation or hue alone.
- **Don't** put functional text below 11px.
- **Don't** introduce elevated cards, filled pill chips, sparklines, gradient accents,
  or a second hero-sized figure. This is a schedule, not a scoreboard.
- **Don't** use Unicode or icon-font glyphs as marks, and don't load a webfont, CDN
  script or external image.
- **Don't** impersonate Razorpay branding or imply an official Razorpay product.
- **Don't** add a dark mode.
