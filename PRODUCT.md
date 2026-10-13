# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary (user decision, 2026-10-05): the **demo audience** of the JNPT case study: capstone
evaluators and stakeholders watching an operator walk through the system. In one glance per
page they must grasp what each agent did, what is waiting on a person, and what the agents
saved. Secondary: import-desk operators at Nhava Sheva (INNSA) who use the same pages to edit
data, review rule changes and approve filings; nothing they can do today may be lost.

## Product Purpose

Port & Customs Clearance Copilot: a locally run multi-agent system that cross-checks an import
shipment's documents against three editable data sources (port demurrage tariffs, FX-rate
rules, an approved compliance rulebook) and produces discrepancy flags, a demurrage/hold-risk
estimate in INR, a clearance checklist and a human-gated clearance workflow. A scheduled agent
watches a simulated customs bulletin and emails a reviewer. Success: a viewer can follow a
shipment from documents to "cleared" and see the money the agents saved, with every figure
traceable.

## Positioning

Agents never decide: they find, price and explain; named people approve rule changes and
filings at explicit gates, and every step lands in an audit trail. The ROI is rebuilt from
figures visible on the other pages, not asserted.

## Operating Context

Streamlit multipage app on a Windows laptop (`streamlit run dashboards/Home.py`), presented live
or screen-shared. Pages: Home, Mission Control, Shipping Ops, Demurrage, FX Rates, Compliance,
Customs Bulletin. Agent runs take 30-60 s per shipment (MCP subprocesses, local retrieval
models, optional Groq LLM). Times are shown in IST; data is stored in UTC. The walkthrough is
`docs/DEMO_SCRIPT.md`.

## Capabilities and Constraints

- Every existing function, data field and control stays; the redesign changes presentation and
  wording only. Widget keys are relied on by the test suite (`tests/test_dashboards/`).
- Stack is fixed: Streamlit 1.64, server-rendered Python pages; custom look via theme config,
  injected CSS and HTML rendered with `st.markdown`; fonts served locally (no runtime network
  except the optional Gmail API).
- Light theme by default; a dark theme chosen with a **custom toggle in the sidebar** (user
  decision), which must restyle every surface, including Streamlit's own widgets and tables.
- Wording in plain operator language (user decision): technical identifiers (run ids, payload
  JSON, agent/module names, row ids) stay available but move into details sections.

## Brand Commitments

- Name: "Port & Customs Clearance Copilot".
- Visual reference pinned by the user: the Muselogist logistics site by Musemind
  (https://muselogist.webflow.io): a cool pale-blue ground, ink-navy type, sky-blue colour
  fields, thin ink outlines with an offset block behind cards and buttons, generous spacing,
  Outfit for text and a soft display serif (Priego, commercial, so a free soft serif stands
  in). Mood: mellow and light, "not engineered".

## Evidence on Hand

All data is synthetic (the JNPT auto-parts fixtures in `tests/fixtures/`, labelled simulated):
four shipments, INNSA tariffs, gazetted/locked/spot FX rates, a six-rule rulebook and a
simulated customs bulletin. There are no real customers, testimonials, logos or benchmarks;
none may be invented. The documented ROI (INR 82,212.00 on the fixtures as of 2026-09-28) is
computed, not a claim.

## Product Principles

1. Show what happened before how it works: outcomes and waiting decisions first, mechanics in
   details.
2. A person decides: make every gate, its reviewer and its consequence unmistakable.
3. Every number explains itself: amounts carry their basis next to them.
4. Calm over clever: quiet pages that let the few things needing attention stand out.

## Accessibility & Inclusion

WCAG AA contrast in both themes; status never conveyed by colour alone (words on every chip);
keyboard focus visible; reduced-motion respected.
