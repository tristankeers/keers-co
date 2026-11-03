---
version: 1
name: keers-co-workbench
description: "A dark research desk. A green-biased black canvas, light warm-grey text and one lime accent. Display and body text set in Geist, labels and figures in Geist Mono, and a single italic serif word (Instrument Serif) in headlines. Figures are always real, sourced and dated; anything missing says so."

colors:
  canvas: "#090b08"
  surface-1: "#10130f"
  surface-2: "#161a14"
  hairline: "#232a20"
  hairline-strong: "#343d2f"
  ink: "#e7ebdf"
  ink-muted: "#8e9887"
  ink-faint: "#5d6658"
  accent: "#c4f25a"
  on-accent: "#0d1205"
  accent-tint: "#1d2810"
  semantic-good: "#7fd8a6"
  semantic-watch: "#f0b54a"
  semantic-severe: "#ff6f61"
  semantic-unknown: "#8e9887"
  label-reported: "#8fb8ff"
  label-derived: "#b9a3f0"
  label-market: "#5ec8c0"
  label-assumption: "#c4f25a"
  label-estimate: "#f0a06b"
  label-stale: "#ff6f61"

typography:
  display:
    fontFamily: Geist
    fontSize: 44px
    fontWeight: 500
    lineHeight: 1.05
    letterSpacing: -1.3px
  section-title:
    fontFamily: Geist
    fontSize: 32px
    fontWeight: 500
    lineHeight: 1.1
    letterSpacing: -0.6px
  accent-word:
    fontFamily: Instrument Serif
    fontStyle: italic
    fontWeight: 400
    note: "One word or short phrase inside a display or section title. Never a whole line."
  body:
    fontFamily: Geist
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.5
  stat:
    fontFamily: Geist
    fontSize: 44px
    fontWeight: 500
    letterSpacing: -1.2px
    fontVariantNumeric: tabular-nums
  label:
    fontFamily: Geist Mono
    fontSize: 11px
    fontWeight: 400
    note: "Lower case. Panel titles, table heads, eyebrows, run ids, timestamps."
  figure:
    fontFamily: Geist Mono
    fontSize: 12px
    fontWeight: 400
    fontVariantNumeric: tabular-nums

radius:
  control: 999px
  input: 6px
  card: 12px

components:
  button-primary: { background: accent, color: on-accent, radius: control }
  button-ghost: { background: transparent, border: hairline-strong, color: ink, radius: control }
  status-pill: { font: label, radius: control, dot: "6px, glows in its own colour" }
  card: { background: surface-1, border: hairline, radius: card }
  table-head: { background: surface-2, font: label, color: ink-muted }
  missing-state: { border: "1px dashed hairline-strong", background: "faint diagonal hatch" }
---

# Keers & Co workbench design

This file describes how the workbench and its previews should look. It is written for people and for
coding agents. The tokens above are the source of truth: `src/keers/web/static/app.css` mirrors them
as CSS variables, and the published preview page uses the same values.

## Overview

The workbench is a research desk, not a marketing site. Most of the screen is dense tables of filed
figures. The style keeps that density readable on a dark canvas and saves colour for meaning.

- **One accent.** Lime marks the brand, the primary action, our own models in charts and anything
  "live" or "ran". It is never used for a judgement about a company.
- **Semantic colour is separate.** Severe, watch, good and unknown have their own colours. A filled
  paper trade is "good", not lime.
- **Every figure is real.** Show only numbers from actual runs, with their source and date nearby.
  A value that was not produced is written as "missing", in the faint ink, never as a dash that
  could be read as zero.

## Colours

The canvas is black with a slight green bias, so the lime accent sits in the same family. Surfaces
step up in two small lifts (`surface-1` for panels, `surface-2` for table heads and tracks).
Hairlines carry structure; shadows are used only for floating things (search results, the
provenance drawer).

The provenance labels (reported, derived, market, assumption, estimate, stale) keep their own
hues because the reader must tell them apart at a glance. Assumptions share the lime because they
are the inputs a person can edit.

## Typography

- Geist for headings and body. Headings are weight 500 with tight tracking.
- Geist Mono, lower case, for labels, table heads, tickers, run ids and every figure. Figures use
  tabular numbers so columns line up.
- Instrument Serif italic for one word in a headline, for emphasis ("Research *workbench*",
  "the gate stays *closed*"). One per headline at most.
- All three load from Google Fonts with system fallbacks, so the workbench still reads correctly
  offline.

## Layout

- A sticky top bar with a translucent canvas background and a hairline below it.
- Panels are cards with a 12px radius, a header row in the label face, and flush tables.
- Pages open with a short hero: a large title, one muted paragraph, and a faint lime glow in the
  top-right corner. The glow is decoration only and never sits behind figures.
- Controls (buttons, segmented toggles, nav, status pills, search) are fully rounded. Inputs use a
  6px radius so they read as fields, not buttons.

## Components

- **Status pill**: a 6px dot plus a lower-case mono label. The dot glows in its own colour: lime for
  live or ran, red for off or blocked, faint for not run.
- **Severity badge**: tinted background, coloured text and a thin border in the same hue. Severe is
  never solid red, so a table full of distress flags stays readable.
- **Stat**: a big Geist number, a mono caption above, a mono source line below. Use it only for
  figures that are the point of the page.
- **Bars**: 8px rounded tracks on `surface-2`. Our models fill in lime with a soft glow; benchmarks
  fill in `hairline-strong`.
- **Missing state**: a dashed card with a faint hatch. It says what is missing, why, and how to
  produce it.
- **Approval ticket** (previews): a paper trade shown as a message with a left rule in lime (filled)
  or red (blocked) and its risk-check result as a pill.

## Do

- Put the source run and date beside every block of figures.
- Keep lime scarce: one primary button per view, our own series in charts, live status.
- Write labels in plain lower-case words people recognise ("net debt", "from 52w high").

## Don't

- Don't add a second accent colour.
- Don't use lime for "good" or "pass".
- Don't show an estimated or invented number to fill a gap.
- Don't make a whole headline italic serif.

## Responsive behaviour

- Below 1000px, two-column layouts stack to one column.
- Below 720px, the top bar hides the subtitle, keyboard hint and data status.
- Wide tables scroll inside their own panel; the page never scrolls sideways.

## Credits

The DESIGN.md format comes from Google Stitch. The idea to keep one in the repo came from the
VoltAgent awesome-design-md collection (MIT). No content was copied from it; this file describes
Keers & Co's own style.
