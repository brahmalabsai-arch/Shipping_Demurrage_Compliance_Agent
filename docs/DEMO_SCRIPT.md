# Demo script — the JNPT auto-parts case study (~20 minutes)

An importer brings four containers of auto parts (HS heading 8708) into Nhava Sheva (`INNSA`).
The demo shows the agents catching document problems before they cost demurrage, a regulatory
change flowing from a customs bulletin to a human reviewer, a clearance that cannot happen
without a person's approval, and the money the agents save — every step traceable in the audit
trail.

**Before you start**

- Set up as in the [README](../README.md) and start the dashboards
  (`.\.venv\Scripts\streamlit.exe run dashboards/Home.py`, open <http://localhost:8501>).
- Use freshly seeded data. If the database has been used before, run the README's
  [Start over](../README.md#start-over) steps first.
- `.env` has `REVIEWERS` set (the steps below call the reviewer "you").
- The demo data is priced for 2026, so every analysis below uses the date **2026-09-28**. The
  figures quoted are for that date and the default ROI assumptions. Rulebook *answers* are
  written by the LLM if `GROQ_API_KEY` is set, otherwise the rule is quoted; either way they
  cite the rule they come from.
- Everything also runs without the dashboards: `python -m scripts.demo_jnpt --as-of 2026-09-28`
  (steps 2 and 7 in one command).

## 1. The data the agents work from (3 min)

Each page edits one data source through the same service layer the agents' MCP servers use.

- **Shipping Ops** — four shipments, all to `INNSA`:

  | Container | Type | From | Status | The story |
  |---|---|---|---|---|
  | MSKU7001001 | 20ft | Shanghai | docked | clean paperwork |
  | MSCU7002002 | 40ft | Busan | docked | packing list says 760, invoice and BoL say 800 |
  | CMAU7003003 | 40ft | Yokohama | at sea | no commercial invoice yet |
  | HLXU7004004 | 40hc | Hamburg | held | consistent papers, but customs is holding it |

- **Demurrage** — INNSA tariffs: e.g. 40ft, 5 free days then USD 90.00/day until 30 Sep; from
  1 Oct 4 free days, USD 110.00/day.
- **FX Rates** → *Customs-notified* — the customs-notified USD/INR rate for 21–30 Sep is **88.40** (a
  revision of the 16–30 Sep notification).
- **Compliance** → *Rulebook in force* — six approved rules (duty on 8708 at 15%, brake
  certificates, quantity consistency, invoice required, exchange-rate rule, Korea CEPA).

## 2. Every agent, every shipment (5 min)

**Mission Control** → *Case study*: set **Analysis date** to `2026-09-28` → **Analyse all 4
shipments** (about a minute per shipment).

For each shipment the document verification agent cross-checks the papers; then, in parallel,
the demurrage & risk agent prices the stay and the compliance agent asks the approved rulebook;
the clearance checklist combines them. Expect:

| Container | Documents | Risk | Demurrage (INR) | Checklist |
|---|---|---|---|---|
| MSKU7001001 | clear | high | 15,912.00 | ready for clearance |
| MSCU7002002 | quantity_mismatch | high | 15,912.00 | not ready (1 blocker) |
| CMAU7003003 | missing_document | medium | 0.00 (not arrived) | not ready (1 blocker) |
| HLXU7004004 | clear | high | 141,440.00 | ready for clearance |

Talking points: HLXU7004004 has been in port since 8 Sep — 21 days, 16 chargeable × USD 100
= USD 1,600 × 88.40. MSCU7002002's blocker comes with the rulebook's guidance (the quantity
rule). The agents never pick a side between two disagreeing documents; they show every value.

Open **Shipping Ops** → MSCU7002002 → *Analysis* → **Analyse shipment** for one
shipment's full checklist with rulebook guidance and references.

## 3. Follow one run in the audit trail (1 min)

**Mission Control** → *Audit trail* → *Follow one run, or one kind of event*: paste the full
run id of MSCU7002002 (from *Run ids, to follow a run in the audit trail* under the case-study
table, or set *Who* to *Document check* and copy one). The run's four steps appear: papers
checked, demurrage and risk assessed, rulebook consulted, checklist built, each with its
findings. Turn on **Refresh every 5 seconds** and leave it on for the next steps.

## 4. Ask the rulebook (1 min)

**Compliance** → *Ask the rulebook*: "What basic customs duty is charged on motor vehicle
parts?" → an answer citing **HSN-8708-duty v1** (BCD 15%). Ask something the rulebook does
not cover — "What is the anti-dumping duty on solar panels from Vietnam?" → **Not found in
current rulebook.** The agent never answers from outside the approved rules. (Both are
questions from the retrieval golden set, checked with the real models by
`pytest -m models`.)

## 5. A regulatory change reaches a human (3 min)

1. **Customs Bulletin** → *Edit*: in the `HSN-8708-duty` section change `15% of the assessable
   value` to `20% of the assessable value` → **Publish**.
2. *Scans and changes* → **Run scheduled scan now**. The regulatory watch agent finds that the
   bulletin now differs from the rule in force, summarises the change, stages **HSN-8708-duty
   v2** as *pending* and emails the reviewer (Gmail if configured, otherwise an `.eml` file in
   `outbox/`).
3. **Compliance** → *Waiting for review*: v2 is shown against the rule in force (diff 15% → 20%).
   Nothing changes until a person decides — the agent can only propose.

## 6. A clearance that needs people (5 min)

1. **Shipping Ops** → MSKU7001001 → *Clearance*: Analysis date `2026-09-28` → **Start
   clearance run**. The supervisor runs the agents and stops at the **compliance gate**: a
   pending rule (v2) covers these goods. The run is saved in Postgres — you could close the
   dashboard now and resume tomorrow.
2. **Compliance** → choose yourself as *Reviewer* (sidebar) → **Approve** v2.
3. Back on **Shipping Ops** → MSKU7001001 → *Clearance* → **Resume run**. The rulebook is
   asked again (the duty answer now cites **v2**, 20%) and the run stops at the **filing
   gate**: the checklist is ready.
4. *Details* → *Move to the next stage* never offers Cleared — only the filing gate can
   clear a shipment. Choose
   yourself as *Reviewer* (sidebar), add a note → **Approve filing**. The shipment moves
   `docked → under_review → cleared`; the run is `filed`.

   (Alternative: **Reject filing** with a reason — the run ends `filing_rejected`, for good;
   the shipment stays docked and a new run would be needed.)
5. In the *Audit trail* → *Follow one run, or one kind of event*, set *Event code starts
   with* to `filing.` or `case_run.` to show who decided what, when.

## 7. The ROI of the agents (3 min)

**Mission Control** → *Savings*:

1. Capture a snapshot: name `demo`, analysis date `2026-09-28`, keep the default assumptions
   (3 days in port with papers in order; customs holds a box 5 days for a quantity mismatch, 7
   for a missing document) → **Capture snapshot**. It freezes the shipments, their documents,
   the tariff and the FX rate that price each one, and the rules in force.
2. Before = `demo`, After = `demo` → **Compare**:

   | Container | Without the agents | With the agents | Avoided (INR) |
   |---|---|---|---|
   | MSCU7002002 | 3 + 5 = 8 days, 3 chargeable × USD 90.00 × 88.40 | 3 days, 0 chargeable | 23,868.00 |
   | CMAU7003003 | 3 + 7 = 10 days, 6 chargeable × USD 110.00 × 88.40 | 3 days, 0 chargeable | 58,344.00 |
   | MSKU7001001, HLXU7004004 | no issues — the same either way | | 0.00 |
   | **Total** | | | **82,212.00** |

3. Rebuild one line by hand from the pages: MSCU7002002 is a 40ft box arriving 22 Sep →
   Demurrage page: 5 free days, USD 90.00 → 3 chargeable days × 90.00 = USD 270.00 → FX page:
   88.40 → INR 23,868.00. *How each figure was built* shows the same arithmetic for every line.
4. The inputs are frozen: change the 40ft tariff on the Demurrage page, come back and
   **Compare** again — the same 82,212.00. A new snapshot captured now would see the change.

## 8. Wrap-up

- Every agent reached its data only through MCP servers, under its own name — visible in the
  audit trail's actor column.
- No rule was approved and no shipment cleared without a named person's decision.
- The ROI number was rebuilt by hand from figures on the dashboards.

To run the demo again from the start, use the README's *Start over* steps. Running the case
study (step 2) again without a reset is fine: every run has its own run id.
