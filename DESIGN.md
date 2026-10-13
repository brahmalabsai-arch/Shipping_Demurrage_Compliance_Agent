---
name: Port & Customs Clearance Copilot
description: A calm import desk for Nhava Sheva, where agents check the papers and people decide.
colors:
  pale-harbour: "#f4f7ff"
  paper-white: "#ffffff"
  mist-blue: "#eaf0fd"
  sky-field: "#deecff"
  sky-block: "#89bcff"
  harbour-ink: "#0e0f18"
  slate-ink: "#3f4558"
  quiet-ink: "#5c6378"
  focus-blue: "#0050bd"
  ok-green: "#1d6b4c"
  warn-amber: "#7a5000"
  stop-coral: "#ad3324"
  night-ground: "#0d1018"
  night-panel: "#151a26"
  night-field: "#192640"
  night-block: "#3d6aa6"
  night-ink: "#eef2fb"
typography:
  display:
    fontFamily: "Young Serif, Georgia, serif"
    fontSize: "clamp(2.1rem, 3.4vw, 3rem)"
    fontWeight: 400
    lineHeight: 1.08
    letterSpacing: "-0.01em"
  figure:
    fontFamily: "Young Serif, Georgia, serif"
    fontSize: "clamp(2.2rem, 4vw, 3.2rem)"
    fontWeight: 400
    lineHeight: 1.05
    fontFeature: "lnum, tnum"
  headline:
    fontFamily: "Young Serif, Georgia, serif"
    fontSize: "1.45rem"
    fontWeight: 400
    lineHeight: 1.2
  lede:
    fontFamily: "Outfit, system-ui, sans-serif"
    fontSize: "1.12rem"
    fontWeight: 400
    lineHeight: 1.55
  body:
    fontFamily: "Outfit, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Outfit, system-ui, sans-serif"
    fontSize: "0.92rem"
    fontWeight: 500
    lineHeight: 1.3
rounded:
  none: "0px"
  pill: "999px"
spacing:
  section: "2.6rem"
  panel: "1.4rem"
  gutter-mobile: "16px"
components:
  button-primary:
    backgroundColor: "{colors.harbour-ink}"
    textColor: "{colors.paper-white}"
    rounded: "{rounded.none}"
    height: "2.6rem"
  button-secondary:
    backgroundColor: "{colors.paper-white}"
    textColor: "{colors.harbour-ink}"
    rounded: "{rounded.none}"
    height: "2.6rem"
  button-secondary-hover:
    backgroundColor: "{colors.sky-field}"
  input:
    backgroundColor: "{colors.paper-white}"
    textColor: "{colors.harbour-ink}"
    rounded: "{rounded.none}"
  focus-panel:
    backgroundColor: "{colors.paper-white}"
    rounded: "{rounded.none}"
    padding: "1.35rem 1.5rem"
  field-panel:
    backgroundColor: "{colors.sky-field}"
    rounded: "{rounded.none}"
    padding: "1.35rem 1.5rem"
  chip:
    rounded: "{rounded.pill}"
    padding: "0.12rem 0.62rem"
  table-header:
    backgroundColor: "{colors.mist-blue}"
    textColor: "{colors.slate-ink}"
---

# Design System: Port & Customs Clearance Copilot

## Overview

**Creative North Star: "The Harbour Brochure That Works"**

The desk borrows the calm of the Muselogist logistics site (pale-blue ground, ink type, sky-blue fields, square shapes with a hard offset block) and turns it into a working tool. A demo audience should see what is waiting on a person and what the agents saved before reading any table. Then they should be able to follow one shipment from papers checked to filed. The pages talk like an operator, not like the code: "Held by customs", "Quantities disagree", "Must fix". Run ids, rule keys and agent module names stay in details.

The density is moderate. Everything sits in one 1240px column with generous section breaks. Whitespace and hairlines group things; cards appear only where the grouping means something. Colour is mostly quiet. The sky blue marks progress and fields. The ink offset block marks the few things that need a person. Status colour always comes with a word.

The design rejects the grey admin panel with one accent colour, and pages made of raw data grids.

**Key Characteristics:**
- Pale-blue ground with white panels and ink hairlines; dark mode mirrors every role.
- Young Serif for page titles, section titles and big figures; Outfit for everything else.
- Square corners everywhere except the status chips, which are pills.
- One signature move, the offset block, used only on what needs a person and on primary actions.
- One product move: the shipment lifecycle drawn as a route line.

## Colors

A cool, mellow harbour palette: pale blues and ink, with semantic colour held back for status.

### Primary
- **Harbour Ink** (#0e0f18): text, hairline edges of focus panels, the primary button fill, the active tab and nav underline. In dark mode it becomes Night Ink (#eef2fb) and the primary button turns sky blue.
- **Sky Block** (#89bcff): the offset block behind focus panels and primary buttons, and the passed stops on the route line. Never used as a text colour on light grounds. Night Block (#3d6aa6) in dark mode.

### Neutral
- **Pale Harbour** (#f4f7ff): the page ground. Night Ground (#0d1018) in dark mode.
- **Paper White** (#ffffff): panels, inputs, tables. Night Panel (#151a26) in dark mode.
- **Mist Blue** (#eaf0fd): the sidebar, table headers and number-input steppers.
- **Sky Field** (#deecff): field panels (e.g. "Run the regulatory watch now"), hover fills, info alerts. Night Field (#192640) in dark mode.
- **Slate Ink** (#3f4558) and **Quiet Ink** (#5c6378): secondary text, captions and labels. Both pass WCAG AA on Pale Harbour.
- **Hairlines**: rgba(14, 15, 24, 0.13) for dividers and rgba(14, 15, 24, 0.30) for input strokes (light-ink equivalents in dark).

### Status (semantic only)
- **OK Green** (#1d6b4c on #e2f3ea), **Warn Amber** (#7a5000 on #fbefd2), **Stop Coral** (#ad3324 on #fde6e2), **Info Blue** (#0b4ea8 on #deecff). Dark mode lightens the text and deepens the fills (e.g. #ff9b8c on #2e1915).

### Named Rules
**The Word-With-Colour Rule.** Status is never shown by colour alone. Every chip, alert and route stop carries its word ("Held by customs", "Must fix").
**The Coral-Is-For-Blockers Rule.** Stop Coral appears only where something blocks clearance or a filing was rejected.

## Typography

**Display Font:** Young Serif (with Georgia, serif)
**Body Font:** Outfit (with system-ui, sans-serif)

**Character:** A soft, slightly old-world serif sits over a round geometric sans. The pair reads like a shipping brochure, not a terminal. Both are bundled under `dashboards/static/fonts` (OFL), so nothing is downloaded at runtime.

### Hierarchy
- **Display** (400, clamp(2.1rem, 3.4vw, 3rem), 1.08): one page title per page.
- **Figure** (400, clamp(2.2rem, 4vw, 3.2rem), 1.05, lining and tabular numerals): the one headline number on a page, such as demurrage avoided.
- **Headline** (400, 1.45rem, 1.2): section titles.
- **Lede** (400, 1.12rem, 1.55, at most 62ch): the sentence under each page title.
- **Body** (400, 1rem, 1.5, at most 70ch for prose).
- **Label** (500, 0.92rem): widget labels, route-stop labels, table headers.

### Named Rules
**The Lining Numerals Rule.** Young Serif's default old-style figures make "0" and "1" read as "O" and "I". Every serif number sets `font-variant-numeric: lining-nums` (plus `tabular-nums` in columns).

## Layout

A single main column, at most 1240px wide, with 5.5rem of top padding under a top navigation bar. The sidebar holds the brand, the dark-mode toggle and the page's "who is acting" fields. Sections are 2.6rem apart. Each opens with a serif headline and an optional one-line caption.

Every page follows the same order: what needs a person first, then the working table, then the forms that change data. Tabs split long pages into tasks ("Waiting for review", "Rulebook in force").

Below 760px, the route line becomes a two-column grid of stops, and tables scroll sideways inside their own frame. The page keeps a 16px gutter and never scrolls horizontally.

## Elevation & Depth

The surfaces are flat, with one structural exception. Panels separate from the ground by tone and a hairline, not by blur shadows. The only shadow is the hard offset block, a solid sky-blue rectangle set down and to the left. It means "a person acts here".

### Shadow Vocabulary
- **Offset block, panel** (`box-shadow: -7px 7px 0 0 var(--block)`): focus panels for pending reviews, runs waiting at a gate, and the savings figure.
- **Offset block, button** (`box-shadow: -4px 4px 0 0 var(--block)`): primary buttons and the primary page link. On hover the button sinks into the block (`translate(-4px, 4px)`, shadow to 0).

### Named Rules
**The One Block Rule.** At most one or two offset blocks per viewport. If everything is elevated, nothing is waiting.

## Shapes

Square corners (0px) on panels, inputs, buttons, tables, tabs, expanders and code blocks. Status chips are the only round shape (999px pill), so a status reads as a token, not a control. Route stops are 13px circles: hollow when not reached, sky when passed, ink with a halo at the current stop.

## Components

### Buttons
- **Shape:** square (0px), at least 2.6rem tall, labels on one line.
- **Primary:** Harbour Ink fill and white text, sitting on the sky offset block. In dark mode the fill is sky blue with ink text.
- **Hover / Focus:** the primary sinks into its block in 160ms `cubic-bezier(0.23, 1, 0.32, 1)`. Secondary buttons fill with Sky Field and their stroke turns ink. Hover is gated to `(hover: hover) and (pointer: fine)`. `:active` scales to 0.98. Focus shows a 2px Focus Blue outline with a 2px offset.
- **Secondary:** a white panel with a 30% ink stroke.

### Chips
- **Style:** pill, 0.12rem by 0.62rem padding, tinted fill with darker text in the same hue (ok / warn / stop / info / quiet).

### Cards / Containers
- **Focus panel** (`st.container(key=theme.focus_key(...))`): white, 1px ink edge, offset block. Use it for what needs a person.
- **Field panel** (`field_key`): Sky Field fill with a hairline. Use it for a standing action with its explanation.
- **Panel** (`panel_key`) and forms: white with a hairline, padding of 1.25 to 1.4rem.

### Inputs / Fields
- **Style:** white (Night Input #0f131d in dark mode), a 30% ink stroke, square.
- **Focus:** the stroke turns ink, with a 2px sky ring (rgba(137, 188, 255, 0.45)).
- **Popovers:** dropdown lists and the calendar take the panel colour, and the selected day is the primary colour. Streamlit 1.6x draws these with react-aria, so the selectors target `*RootElement`, `[role="group"]`, `*VirtualDropdown` and `stDateInputCalendar`.

### Tables
`st.table` through `theme.table`, never the canvas data grid, because the grid cannot follow the theme. Headers are Mist Blue with Slate Ink 500-weight text. Rows have hairlines and a Sky Field hover tint. Numbers use tabular figures.

### Navigation
A top bar (`st.navigation(position="top")`) with Material icons. The current page is ink, 600 weight, with a 2px ink underline. Other pages are Slate Ink with a Sky Field hover. The pages live in `dashboards/views/`, not `pages/`, so Streamlit never shows its automatic sidebar list.

### Route line (signature)
`theme.route(stops, here=...)`: the lifecycle from Quoted to Closed as a hairline with circles. Each stop shows its count (on Overview) or its state (on a shipment) above a label. Side states such as held or cancelled sit beside it as chips.

### Dark mode toggle
A sidebar toggle, kept in session state and also reachable as `?theme=dark`. It swaps every token at once. The track is a 30% ink line when off and sky blue when on.

## Do's and Don'ts

### Do:
- **Do** write labels and messages in operator words ("Quantities disagree", "Waiting for filing approval"), and put internal ids in an expander.
- **Do** put what needs a person in a focus panel at the top of the page, before tables.
- **Do** add every new colour as a token in both `LIGHT` and `DARK` in `dashboards/theme.py`. A test checks that the two sets match.
- **Do** set serif numbers with lining figures.
- **Do** show tables through `theme.table` so they follow the theme.

### Don't:
- **Don't** use `st.dataframe` on a page. Its canvas ignores the theme and breaks dark mode.
- **Don't** signal status by colour alone.
- **Don't** round corners on panels, inputs or buttons. Only chips are pills.
- **Don't** add blur shadows or gradients. Depth is tone, hairlines and the one offset block.
- **Don't** use em dashes or Python class names in visible copy.
- **Don't** put more than two offset blocks in one viewport.
