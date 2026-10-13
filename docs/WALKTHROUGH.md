# Walkthrough: fifteen shipments, every way to stop one, every way to clear it

This guide is for learning the whole desk by doing. Fifteen shipments are seeded, each stuck
(or not) for a different reason. For each one you will see what the agents report, understand
*why* the system stops there, fix it the way an operator would, and take it to **Cleared**.

Every case is also played from problem to Cleared by an automated test
(`tests/test_e2e/test_walkthrough.py`), so the steps below are known to work.

---

## 0. Before you start

1. Load the data once (skip what you already did):

   ```powershell
   .\.venv\Scripts\python.exe -m scripts.seed               # base data: tariffs, rates, rulebook
   .\.venv\Scripts\python.exe -m scripts.seed_walkthrough   # the 15 cases below
   .\.venv\Scripts\python.exe -m streamlit run dashboards/Home.py
   ```

2. On **Shipping Ops**, in the sidebar, choose yourself under **Reviewer (for filing
   decisions)**. On **Compliance**, choose yourself under **Reviewer (recorded with each
   decision)**. Nothing can be approved without a named reviewer.
3. Open a case: **Shipping Ops → Shipments → Open shipment**, and type the container number
   (or part of the cargo, e.g. *door hinges*). The full list of shipments is folded under it.
4. Keep the **Analysis date** at today's date. The figures in this guide are for
   **5 Oct 2026**; on a later day the days in port (and the demurrage) grow, the decisions do not
   change.
5. A clearance run takes about a minute (the agents read the rulebook with a language model).
   The page keeps your tab while it works and shows the result in a note in the corner.

**Replaying a case.** The seed never overwrites what you did. To start over from scratch:
`python -m scripts.setup_local_db --reset` (asks for the postgres password, wipes everything),
then both seed commands again.

---

## 1. How the desk decides (read this once)

### The journey of a shipment

```
Quoted → Booked → In transit → Docked → Under review → Cleared → Closed
                                  │  ↑
                                  ↓  │
                          Held by customs
```

You move a shipment along this line by hand on **Details → Move to the next stage**, with one
exception: **Cleared is never a manual move.** Only an approved filing (below) clears a box.

### Two ways to look at a shipment

| | **Analysis tab** | **Clearance tab** |
|---|---|---|
| What runs | the four agents | the same four agents, then two human gates |
| Changes anything? | no, a dry run | yes, can file the shipment and clear it |
| Use it to | see the problems | take the shipment to Cleared |

### The four agents (they run in this order)

1. **Document check** compares the commercial invoice, packing list and bill of lading with each
   other and with the booking (quantity, value, currency, dates), checks every document is
   there once the goods are on board, and finds the exchange rate for each date
   (quote, bill of lading, invoice).
2. **Demurrage & risk** prices the time in port: the tariff in force on the **arrival day**
   (the arrival day counts as day 1), free days, then a daily charge, converted to rupees at the
   customs-notified rate. It rates the risk: **high** while charges accrue or customs holds the
   box, **medium** when a hold looks likely, **low** otherwise.
3. **Rulebook** asks the approved rulebook a fixed set of questions about the goods (duty,
   origin, documents, exchange rate, special requirements) and cites the rule that answers.
4. **Checklist** puts everything into one list, each item with a priority:

| Priority on screen | Meaning | Comes from |
|---|---|---|
| **Must fix** (blocker) | the shipment cannot be filed | missing document; quantities, values or currencies that disagree; an invalid currency; **Missing data** (no arrival date, no port tariff, no customs exchange rate) |
| **To do** (action) | worth doing, does not stop filing | date mismatch, expired quote, an exchange rate not locked, high or medium demurrage risk |
| **Note** (info) | for your information | the rulebook's answers, low risk |

**Ready to file** = no Must-fix item, every agent finished, and no contradiction between them.
The rulebook only *advises*: its answers are Notes. Judging them is the reviewer's job.

### The two human gates of a clearance run

1. **Rule gate.** If a *proposed* rule change that covers the goods is waiting for review, the run
   stops: **Waiting for a rule decision**. A reviewer decides it on Compliance; then you press
   **Resume run**. An approval makes the agents ask the rulebook again.
2. **Filing gate.** If the checklist is ready **and** the box is **Docked** or **Under review**,
   the run stops: **Ready to file: waiting for approval**. **Approve filing** clears the
   shipment; **Reject filing** ends the run.

### How a run can end

| Result on screen | Why | What you do |
|---|---|---|
| **Filed and cleared** | approved at the filing gate | nothing, done |
| **Blocked by the checklist** | a Must-fix item; or the papers changed after the analysis | fix it, start a **new** run |
| **Not at a fileable stage** | the box is not Docked or Under review | move it, start a new run |
| **Filing rejected** | a reviewer said no | address the reason, start a new run |

A finished run is never reopened: every retry is a new run, listed under **Runs for this
shipment**, and every step is in **Mission Control → Audit trail**.

---

## 2. The cases at a glance

| Case | Container | What stops it | Where you fix it |
|---|---|---|---|
| WT01 | OOLU8101010 | nothing | Clearance |
| WT02 | EGHU8102020 | packing list short by 30 | Papers → amend |
| WT03 | TGHU8103030 | invoice value ≠ booking | Details → edit booking |
| WT04 | CMAU8104040 | invoice in USD, booking in EUR | Papers → amend |
| WT05 | HLXU8105050 | currency typo "USS" | Papers → amend |
| WT06 | MSCU8106060 | no bill of lading | Papers → attach |
| WT07 | ONEU8107070 | still "In transit" | Details → move stage |
| WT08 | YMLU8108080 | held by customs | Details → move stage |
| WT09 | SEGU8109090 | no tariff for a 20ft reefer | Demurrage page |
| WT10 | ZIMU8110100 | no arrival date | Details → edit booking |
| WT11 | BMOU8111110 | a new rule for its goods | Customs Bulletin + Compliance |
| WT12 | TCNU8112120 | reviewer rejects the filing | Papers → attach, new run |
| WT13 | FCIU8113130 | papers change while waiting | new run |
| WT14 | GESU8114140 | only a quotation | the whole lifecycle |
| WT15 | TRHU8115150 | only To-do items | nothing (optional tidy-up) |

---

## 3. The cases

### WT01 · OOLU8101010 · The straight path

*Door hinges from Shanghai, 20ft, USD 36,000. Docked 4 Oct; the papers agree.*

**What you'll see.** Analysis: low risk, "in free time until 7 Oct" (INNSA 20ft from 1 Oct: 4
free days, then USD 55 a day; 2 days in port). Checklist: **Ready to file: 0 must-fix,
0 to do**, only Notes (*Rulebook answer*, with the rules cited).

**Steps.**
1. **Clearance → Start clearance run.** → *Waiting for filing approval.* The panel
   **Ready to file: waiting for approval** shows the checklist again.
2. Add a note if you like → **Approve filing.** → *Filed and cleared.*

**Notice.** The route line jumps from Docked to **Cleared**: on approval the supervisor moves
the box to Under review and files it in one go. Look at **Runs for this shipment**: one run,
*Filed and cleared*, with who approved it.

---

### WT02 · EGHU8102020 · Packing list short

*Drive axles from Ningbo, 40ft, 500 units, USD 145,000. Docked 30 Sep.*

**What you'll see.** Must fix: **Quantities disagree**, "500 on commercial invoice, bill of
lading, shipment record vs 470 on packing list (delta 30)". The agent lists every value and where
it read it; it never picks a winner. Risk **high**: the Sep tariff (5 free days, USD 90/day)
started charging on 5 Oct, USD 90 so far (INR 7,969.50). The rulebook's
*DOC-quantity-consistency* rule is attached to the item as guidance.

**Steps.**
1. **Clearance → Start clearance run** → *Blocked by the checklist* (the Note column of the run
   names the blocker).
2. **Papers → Amend a document**: Document *Packing list v1*, Qty **500** → **Issue amended
   version.** (The old version is kept; tick *Show replaced versions* to see it.)
3. **Clearance → Start clearance run** → *Waiting for filing approval* → **Approve filing.**

**Notice.** You could equally have amended the invoice and bill of lading and the booking to 470:
the system only demands that the papers agree, not which number is true. That judgement is
yours.

---

### WT03 · TGHU8103030 · Invoice value differs from the booking

*Radiators from Hamburg, 40ft high-cube. Booked at EUR 61,200; the invoice says EUR 64,800.*

**What you'll see.** Must fix: **Values disagree**, delta EUR 3,600. Risk **high**: arrived
28 Sep, 3 chargeable days × USD 100 = USD 300 (INR 26,565.00).

**Steps.**
1. **Details → Edit the booking**: Declared value **64800** → **Save changes.**
2. **Clearance → Start clearance run → Approve filing.**

**Notice.** The invoice is the commercial truth here (a price change after booking), so the
booking follows it. The demurrage keeps growing every day the box waits: the checklist shows it
as **To do**, not Must fix. Cost is a reason to hurry, not a reason to refuse.

---

### WT04 · CMAU8104040 · Invoice in the wrong currency

*Clutch kits from Genoa, 20ft. Booked at EUR 52,500; the supplier issued the invoice in USD.*

**What you'll see.** Must fix: **Currencies disagree** (USD on the invoice vs EUR on the
booking). *Check the papers only* shows "no rate resolved: documents disagree on the currency":
until the currency is settled nothing is converted, and **values are not compared at all**.

**Steps.**
1. **Papers → Amend a document**: *Commercial invoice v1*, Currency **EUR** → **Issue amended
   version.**
2. **Clearance → Start clearance run → Approve filing.**

**Notice.** The checks are layered. Once the currency agrees, the value check runs. Here the
amounts also agree, so the run goes through; had they differed, a new **Values disagree** would
appear only now.

---

### WT05 · HLXU8105050 · Currency code typo

*Seat belts from Ho Chi Minh City, 40ft. The invoice currency reads "USS".*

**What you'll see.** Must fix: **Unknown currency**, "'USS' is not an ISO 4217 currency code;
nothing in it can be converted until it is corrected". Risk **medium** only because of that
problem (in free time until 8 Oct: your 40ft tariff from 1 Oct gives 6 free days).

**Steps.** **Papers → Amend**: *Commercial invoice v1*, Currency **USD** → **Issue amended
version** → **Clearance → Start clearance run → Approve filing.**

**Notice.** "USS" passes the form (three capital letters) but not the agents: the format check
and the meaning check are different layers.

---

### WT06 · MSCU8106060 · Bill of lading missing

*Exhaust parts from Laem Chabang, 40ft high-cube. Docked 1 Oct with an invoice and a packing
list only.*

**What you'll see.** Must fix: **Document missing**, "Bill of lading missing: required once the
goods are on board". Risk **high**: 1 chargeable day at USD 120 (INR 10,626.00).

**Steps.**
1. **Papers → Attach a document**: Document **Bill of lading**, Qty **1100**, Value empty,
   Currency empty, Issued on **2026/09/05** → **Attach.**
2. **Clearance → Start clearance run → Approve filing.**

**Notice.** Issue it on **5 Sep**, the booking's on-board date. Any other date passes (it is
only a To-do) but adds a *Dates disagree* item: the bill of lading exchange rate is locked to the
booking's on-board date.

---

### WT07 · ONEU8107070 · Still in transit

*Alloy wheels from Shanghai, 40ft. Expected 3 Oct; the record still says In transit.*

**What you'll see.** Ready to file, nothing to fix, risk **medium**: "ETA 3 Oct has passed but
the shipment is not docked". Starting a clearance run ends **Not at a fileable stage**:
"only docked or under-review shipments are filed".

**Steps.**
1. **Clearance → Start clearance run** → *Not at a fileable stage* (to see it).
2. **Details → Move to the next stage**: *From in transit to* **Docked** → **Move.**
3. **Clearance → Start clearance run → Approve filing.**

**Notice.** After docking the risk drops to **low** (3 days in port, 6 free days). The arrival
day is taken from the ETA: the record has no separate "docked on" date.

---

### WT08 · YMLU8108080 · Held by customs

*Bumpers from Qingdao, 20ft. Arrived 25 Sep, papers fine, customs is holding it.*

**What you'll see.** Risk **high**: "held by customs: charges keep accruing", 6 chargeable days ×
USD 45 = USD 270 (INR 23,908.50). The run ends **Not at a fileable stage** ("held").

**Steps.**
1. **Details → Move to the next stage**: *From held by customs to* **Under review** → **Move.**
   (A held box can only go back to review: the hold is lifted.)
2. **Clearance → Start clearance run → Approve filing.**

**Notice.** The route line shows a held box at Docked with a *Held by customs* chip. Once under
review it files straight to Cleared.

---

### WT09 · SEGU8109090 · A reefer with no port tariff

*Temperature-controlled airbag modules from Tokyo, 20ft reefer (20rf), JPY. Docked 2 Oct.*

**What you'll see.** Must fix: **Missing data: port tariff**, "no demurrage tariff for INNSA /
20rf on 2026-10-02". Risk **Risk unknown**: the stay cannot be priced, and the system refuses to guess a
zero. The papers and exchange rates are fine.

**Steps.**
1. **Demurrage → Add or revise a tariff**, Start from *(a new tariff period)*: Port **INNSA**,
   Container type **20rf**, Currency **USD**, Free days **3**, Charge per day after that
   **95.00**, Applies from **2026/01/01**, Applies until empty → **Save tariff.**
2. **Shipping Ops → Clearance → Start clearance run → Approve filing.** The run now prices
   the stay: 4 days in port, 1 chargeable × USD 95.

**Notice.** *Applies from* must be **on or before the arrival day** (2 Oct). The date picker
starts at today, which is too late: a tariff from 5 Oct leaves the gap open (the test checks
this). The tariff in force *on the arrival day* prices the whole stay.

---

### WT10 · ZIMU8110100 · No arrival date

*Shock absorbers from Mersin, 40ft. Docked, but nobody recorded when it arrived.*

**What you'll see.** Must fix: **Missing data: arrival date**, "not set on the shipment record,
so the arrival day is unknown". **Risk unknown.**

**Steps.**
1. **Details → Edit the booking**: *Arrival (clear if unknown) date (IST)* **2026/10/01**,
   time **09:00** → **Save changes.**
2. **Clearance → Start clearance run → Approve filing.** Risk **low**: 5 days in port of 6 free.

**Notice.** The arrival day drives both *which* tariff applies and *how many* days are charged,
so without it there is nothing honest to say about cost.

---

### WT11 · BMOU8111110 · A new rule for its goods waits for review

*LED headlamps from Shenzhen, heading 8512, 40ft high-cube. The rulebook has no rule for 8512
yet: the duty and special-requirements answers say "Not found in current rulebook".*

This case uses the **regulatory watch**: the customs authority publishes a new rule in the
bulletin, the watch finds it, proposes it, and every clearance run for goods it covers waits.
**Do the steps in this order** (a run started before the rule is proposed does not wait).

**Steps.**
1. **Customs Bulletin → Edit**: add this section at the very end of *Bulletin text* →
   **Publish.**

   ```
   ## HSN-8512-lamp-approval
   Effective: 2026-10-01
   HS codes: 8512
   Lighting and signalling equipment for motor vehicles (heading 8512) is cleared only against a
   type-approval certificate from a notified testing agency for each lamp model on the invoice.
   Consignments without it are examined before assessment.
   ```

2. **Scans and changes → Run scheduled scan now.** The note says *1 proposal(s) pending review
   on the Compliance page, 1 review email(s) sent*: the reviewer gets a real email. The report
   may also list *HSN-870830-marking: removed from the bulletin*; that rule was approved by hand
   earlier and is not in the bulletin; it is flagged, not changed.
3. **Shipping Ops → BMOU8111110 → Clearance → Start clearance run** → *Waiting for a rule
   decision*. The focus panel names **HSN-8512-lamp-approval, version 1 · Pending**.
4. **Compliance → Waiting for review (1)**: the card shows the rule as *a new rule*, *Proposed
   by: Regulatory watch (bulletin scan)*, and the run waiting on it. **Approve.**
5. **Shipping Ops → Clearance → Resume run.** The rulebook is asked again (the new rule is now
   in force) → *Waiting for filing approval* → **Approve filing.**

**Notice.** The rule covers heading 8512 only: start a run for any 8708 case meanwhile and it
goes straight to filing. **Reject** instead of Approve and the run carries on under the rulebook
in force; a rejection is final for that proposal. The whole chain (published, scanned, emailed,
proposed, waited, approved, resumed, filed) is in **Mission Control → Audit trail**.

---

### WT12 · TCNU8112120 · The reviewer rejects the first filing

*Gearbox housings from Busan, Korea, 40ft. The papers agree; no certificate of origin is on file.*

**What you'll see.** Ready to file. Among the Notes, the rulebook's answer on preferential duty
cites **ORG-KR-CEPA**: Korean goods get the lower CEPA duty only with a Korean certificate of
origin. The system does not check certificates; the reviewer does.

**Steps.**
1. **Clearance → Start clearance run** → *Waiting for filing approval.*
2. Note: *No Korean certificate of origin on file* → **Reject filing.** → *Filing rejected*.
   The shipment stays Docked; that run is over for good.
3. **Papers → Attach a document**: Document **Certificate**, Issued on **2026/09/01**,
   everything else empty → **Attach.**
4. **Clearance → Start clearance run** (a new run) → **Approve filing.**

**Notice.** Rejection is a human "no" that is never turned into a yes: the old run stays
*Filing rejected* in the history, and the clearance comes from a second run with its own
approval.

---

### WT13 · FCIU8113130 · The papers change while the filing waits

*Brake pads and discs from Ningbo, 20ft, heading 8708 30.*

Brakes have their own rules. See **Compliance → Rulebook in force**:
*HSN-870830-brake-certification* (cleared only against a certificate of conformity), or ask it
on **Ask the rulebook**: "What do brake parts need for clearance?"

**Steps.**
1. **Clearance → Start clearance run** → *Waiting for filing approval.* Leave it waiting.
2. The certificate of conformity arrives: **Papers → Attach a document**: **Certificate**,
   Issued on **2026/09/25** → **Attach.**
3. Back on **Clearance → Approve filing.** → *Blocked by the checklist*; the run's note says
   "the documents changed after the analysis the filing was approved on" and that nothing was
   filed. The box stays Docked.
4. **Clearance → Start clearance run** (a new run, which sees the certificate) → **Approve
   filing.**

**Notice.** An approval is for the papers that were analysed. If anything changes in between,
nothing is filed. This protects against approving one set of papers and filing another.

---

### WT14 · GESU8114140 · From quotation to clearance

*Steering wheels from Shanghai, 40ft, USD 66,000. Only quoted on 15 Sep (valid 7 days); no papers
yet; the ship is due 4 Oct.*

**What you'll see.** To do: **Quote expired** ("valid through 22 Sep, but the shipment is still
only quoted on 5 Oct"); risk **medium** ("ETA has passed but the shipment is not docked"). No
documents are required yet: they are only due once the goods are on board. A clearance run ends
**Not at a fileable stage** (quoted).

**Steps** (the whole lifecycle):
1. **Details → Edit the booking**: Invoice date **2026/09/18**, On-board date
   **2026/09/20** → **Save changes.** (The quote is now checked on the invoice date, 18 Sep,
   which is inside its validity.)
2. **Papers → Attach a document**, three times:
   - Commercial invoice: Qty **1200**, Value **66000**, Currency **USD**, Issued on **2026/09/18**
   - Packing list: Qty **1200**, Issued on **2026/09/18**
   - Bill of lading: Qty **1200**, Issued on **2026/09/20**
3. **FX Rates → Locked → Lock a rate**, twice:
   - Pair **USD/INR**, Locked for **Commercial invoice**, On **2026/09/18**, Rate **88.21**
   - Pair **USD/INR**, Locked for **Bill of lading**, On **2026/09/20**, Rate **88.26**

   (Optional: without them the run still files, with two *No FX rate* To-do items.)
4. **Details → Move to the next stage** three times: **Booked**, **In transit**, **Docked.**
5. **Clearance → Start clearance run** → nothing to fix, nothing to do → **Approve filing.**

**Notice.** The documents are checked against the booking you just completed, and the rates are
looked up on the exact dates the papers carry. A locked rate can never be edited, only added.

---

### WT15 · TRHU8115150 · Only To-do items

*Wiper systems from Xiamen, 20ft. The invoice is dated 3 Sep, the booking says 2 Sep; no rates
are locked for its invoice and bill of lading dates.*

**What you'll see.** **Ready to file: 0 must-fix, 4 to do**: **Dates disagree** (1 day),
**No FX rate** twice (the invoice date and the bill of lading date), and **Demurrage: medium
risk** (the open items make a hold more likely).

**Steps.** **Clearance → Start clearance run → Approve filing** (note: *filed with the loose
ends noted*). Cleared.

**Optional tidy-up first:** amend the invoice's Issued on to **2026/09/02**, and lock USD/INR
rates for the commercial invoice on 2 Sep and the bill of lading on 10 Sep; a new analysis then
shows no To-do items.

**Notice.** To-do items are the agents' advice; only Must-fix items stop a filing. Whether to
file with loose ends is the reviewer's call, recorded with their name.

---

## 4. After the walkthrough

* **Mission Control → Audit trail.** Set *Who* to *Supervisor*, or open *Follow one run, or one
  kind of event* and paste a run id from *Runs for this shipment*, to read a run step by step:
  papers checked, demurrage assessed, rulebook consulted, checklist built, gate waited, decision,
  filing.
* **Overview** counts every shipment by stage; the fifteen move to Cleared as you go.
* **Mission Control → Case study → Analyse all** now analyses all 19 shipments (about a
  minute each).

## 5. If something looks off

| You see | Why | Do |
|---|---|---|
| *Missing data: fx_rate: no gazetted USD/INR rate* | analysis date after 31 Oct 2026 (the customs-notified rates end there) | **FX Rates → Customs-notified**: publish the next period |
| *… did not finish; the checklist is built without it* | an agent failed (often the language model's rate limit) | wait a minute, start a new run |
| *A run for this shipment is still running* | a window closed mid-run | **Clearance → Abandon this run** (needs a reviewer and a reason), then start again |
| **Approve** is greyed out, or missing with a *Choose yourself as Reviewer* note | no reviewer chosen | choose yourself in the sidebar |
| A case behaves differently from this guide | you changed a tariff or rate it relies on | check Demurrage / FX Rates, or reset and reseed |

## 6. What the fifteen cases cover, and what they do not

**Covered:** every Must-fix kind (quantity, value, currency, invalid currency, missing document,
both data gaps: no tariff and no arrival date), every To-do kind (date mismatch, expired quote,
unlocked rate, demurrage risk), both non-fileable stages (in transit, held), the rule gate with
the regulatory watch, an approval, a rejection, papers changed after approval, and the full
quote-to-clearance lifecycle.

**Not staged as cases** (try them on any shipment):

* **Cancelled.** Possible before docking (**Details → Move: Cancelled**); a cancelled shipment is
  never analysed or cleared.
* **Agents that disagree.** The checklist flags it when the papers block clearance but the risk
  agent says low. The risk agent always raises the risk when papers have problems, so it cannot
  happen with the agents as built; it is a guard.
* **Revoking a rule.** **Compliance → Rulebook in force → Revoke an approval**; the version it
  replaced comes back in force, and runs waiting on that rule can resume.
