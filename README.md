# Port & Customs Clearance Copilot

**AI agents for an import desk that catch paperwork problems before a container docks, so
customs does not hold the box and the port does not charge for the wait. People still make
every decision that matters.**

![A shipment taken from analysis to Cleared: the agents check it, a clearance run stops for a
reviewer, the filing is approved](docs/media/02-straight-path.gif)

## The problem

Picture the import desk of an auto-parts company bringing containers into Nhava Sheva (JNPT),
near Mumbai. Every container that lands starts a clock. The port gives a few free days, then
charges **demurrage** for every extra day the box sits there: in this project's demo tariff a
40ft box gets 5 free days, then USD 90 a day.

Whether the box leaves on time mostly depends on paperwork. Customs assesses the import only
when the papers tell one consistent story:

- the **commercial invoice**, the **packing list** and the **bill of lading** must agree with
  each other and with the booking: quantities, values, currency;
- every one of them must be on file once the goods are on board;
- the value has to be converted into rupees at the **right exchange rate**, and the rule
  changes with the stage: a quote uses the day's spot rate, the bill of lading and the invoice
  lock the rate on their own dates, port charges use the rate customs notifies for the period;
- the goods must meet the **customs rules in force**, and those change: the authority
  publishes revised duty rates and requirements in its bulletin.

When something does not match (a packing list that counts 760 gearboxes against an invoice that
says 800, a missing invoice, a currency typed wrong), customs **holds the box** until it is
explained, which takes days (this project's model assumes 5 for a quantity mismatch and 7 for a
missing invoice), and demurrage keeps running the whole time.

Without help, a desk checks all of this by hand, across emails, spreadsheets and portals, often
after the box has docked, which is exactly when every day of delay starts to cost money. Rule changes
are noticed late or not at all. And when someone asks *why* a shipment was filed, or held, the
answer is scattered across inboxes.

## How the copilot helps

The copilot checks every shipment as soon as its papers exist, before the box docks, and keeps
a person in charge of every decision:

1. **Checks the papers.** It compares the invoice, packing list and bill of lading with each
   other and with the booking, lists every value it found and where it found it, and flags what
   disagrees. It never picks a winner between two documents: that is a human call.
2. **Prices the time in port.** It applies the port tariff in force on the arrival day and the
   customs exchange rate, so the risk is a number in rupees, not a feeling.
3. **Asks the rulebook.** It answers duty and document questions from the approved customs
   rules only, and cites the rule it used.
4. **Writes one checklist.** Everything above becomes a single list sorted into *Must fix*
   (customs will not assess past it), *To do* and *Note*.
5. **Watches for rule changes.** On a schedule (every 15 minutes by default) it reads the
   customs bulletin. When a rule changes it emails the reviewer and proposes the change. It
   never applies a rule on its own.
6. **Waits for people at two gates.** A named reviewer approves rule changes, and a named
   reviewer approves every filing. A shipment is never cleared without that approval.
7. **Records everything.** Every agent step and every human decision lands in an audit trail:
   who decided what, when, and on which version of the papers.

## What it is worth

The copilot pays for itself in **demurrage that is never charged**. If a paperwork problem is
caught before the box docks, it is fixed while the ship is still at sea, and the box clears in
its normal time instead of sitting in a customs hold.

The Savings view in Mission Control computes this from the system's own data, not from a
hard-coded estimate. In the demo case study (four containers, analysed on 28 Sep 2026), two
shipments have paper problems:

| Container | Problem | Without the copilot | With it | Demurrage avoided |
|---|---|---|---|---|
| MSCU7002002 | packing list counts 760, the invoice 800 | 8 days in port (3 + a 5-day hold), 3 charged | 3 days, none charged | INR 23,868 |
| CMAU7003003 | commercial invoice missing | 10 days in port (3 + a 7-day hold), 6 charged | 3 days, none charged | INR 58,344 |
| two others | papers in order | same either way | | INR 0 |
| **Total** | | | | **INR 82,212** |

Every figure can be rebuilt by hand from the dashboards: for MSCU7002002, 3 chargeable days ×
USD 90 (the Demurrage page) × 88.40 (the customs rate on the FX Rates page) = INR 23,868. The
assumptions behind it (a box with clean papers clears in 3 days; customs holds a box 5 days for
a quantity mismatch and 7 for a missing document) are shown next to the result and can be
changed.

**At the scale of a desk** (an illustration, not a measurement): with the demo tariffs, one
avoided hold on a 40ft box is worth INR 24,000 to 58,000, the two cases above. A desk clearing
50 containers a month where 1 in 10 has a paper problem would avoid about **INR 1.2 to 2.9 lakh
of demurrage a month**. Put your own volumes, tariffs and hold times in and the Savings view does
the same arithmetic on them.

The money is the easy part to count. The rest matters as much:

- **Time:** a full check of one shipment (papers, rates, rules, checklist) takes about a minute
  instead of an operator's cross-checking across documents and portals.
- **No missed rule changes:** the bulletin is read on a schedule, and a change reaches the
  reviewer by email within minutes, already summarised and staged for a decision.
- **Decisions you can defend:** every filing names its approver and the exact papers it was
  approved on. If the papers change after an approval, nothing is filed.
- **No accidental filing:** a shipment with a must-fix problem, missing data or an undecided rule
  change cannot reach *Cleared*.

## What this is, and is not

A working demo for one import desk, run locally on synthetic data: the shipments, tariffs,
exchange rates, customs bulletin and rulebook are illustrative, not official. It is not
connected to the customs system (ICEGATE) or a port system, and "cleared" here is an internal
decision record, not a real customs filing. The agents' explanations use a language model
(Groq) when a key is configured; every check, price and decision works without it.

## See it work

Real sessions on the dashboards, recorded on a freshly seeded copy of the demo data: real
clicks, the agents really running (their waits are shortened), each step captioned at the
bottom. The cases (WT01, WT02, ...) are the ones in [`docs/WALKTHROUGH.md`](docs/WALKTHROUGH.md),
where every step is written out.

**A tour of the desk.** Overview, then each page, then dark mode.

![A tour of every page of the dashboards, ending in dark mode](docs/media/01-tour.gif)

**The straight path (WT01).** The GIF at the top: open a shipment, analyse it (a dry run:
nothing changes), start a clearance run, approve the filing, Cleared.

<details>
<summary><b>Fix the papers (WT02)</b>: a run is blocked because the packing list counts 470 and
everything else 500; amend it, refile.</summary>

![A blocked clearance run, the papers check showing the quantity mismatch, an amended packing
list and a new run that is approved](docs/media/03-fix-the-papers.gif)

</details>

<details>
<summary><b>Missing data (WT09)</b>: a 20ft reefer with no port tariff, so the stay cannot be
priced; add the tariff on the Demurrage page, refile.</summary>

![Risk unknown for a reefer container, the tariff added on the Demurrage page, then a run that
prices the stay and is approved](docs/media/04-missing-data.gif)

</details>

<details>
<summary><b>A new rule waits for review (WT11)</b>: the bulletin publishes a rule, the regulatory
watch proposes it, the clearance run waits until a reviewer approves it.</summary>

![The bulletin edited and scanned, a clearance run waiting for the rule decision, the rule
approved on Compliance, the run resumed and filed](docs/media/05-rule-gate.gif)

</details>

<details>
<summary><b>A rejected filing (WT12)</b>: the reviewer rejects with a reason; the certificate is
attached and a new run is approved.</summary>

![A filing rejected for a missing certificate of origin, the certificate attached, a new run
approved](docs/media/06-reject-and-refile.gif)

</details>

<details>
<summary><b>Held by customs (WT08)</b>: a held box is not fileable; once the hold is lifted it goes
back under review and is filed.</summary>

![A clearance run ending not fileable for a held box, the box moved back under review, a new run
approved](docs/media/07-held-by-customs.gif)

</details>

<details>
<summary><b>Every step on record</b>: the clearance runs, the audit trail filtered to the
supervisor, and the counts on Overview.</summary>

![Mission Control's clearance runs and audit trail, then the Overview route with the cleared
shipments](docs/media/08-audit-trail.gif)

</details>

## How it is built

A locally run multi-agent system (LangGraph + MCP). Five agents (document verification,
demurrage & risk, compliance RAG over the approved rulebook, the clearance checklist, and a
scheduled regulatory watch) work through six MCP servers over one shared service layer, so the
dashboards, the agents and the command line can never disagree about the data. A LangGraph
supervisor runs the pipeline behind the two human gates and checkpoints every run in
PostgreSQL, so a run can wait for a reviewer across days and restarts. Data lives in a native
PostgreSQL; the rulebook search (BM25 + BGE embeddings + reranking) runs in an embedded
ChromaDB; the dashboards are Streamlit; real review emails go through Gmail (OAuth).

- Specs: [`docs/`](docs/), starting with [`problem_statement.md`](docs/problem_statement.md)
  and [`architecture.md`](docs/architecture.md).
- The JNPT case study, click by click: [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md).
- Every path to clearance, as fifteen hands-on cases: [`docs/WALKTHROUGH.md`](docs/WALKTHROUGH.md).
- The dashboards' design system: [`DESIGN.md`](DESIGN.md).

## What you need

- **Python 3.11 or newer** (developed on 3.13).
- **PostgreSQL 15 or newer**, installed natively and running on `localhost:5432` (developed on
  18), and the password of its superuser (`postgres`) — used once, by the setup script.
- About **3 GB** of disk and an internet connection for the first install (CPU PyTorch and
  the two retrieval models, ~525 MB, downloaded on first use into the Hugging Face cache).
- Optional: a **Groq API key** (free tier) for LLM-written explanations and answers — without
  one every agent uses its deterministic fallback; a **Google Cloud project** for real email
  ([`docs/setup_gmail.md`](docs/setup_gmail.md)) — without it emails are written as `.eml`
  files to `outbox/`.

No Docker: ChromaDB runs inside the Python process; `docker-compose.yml` is only for GitHub CI.
Commands below are for Windows PowerShell; on macOS/Linux use `.venv/bin/python` instead of
`.\.venv\Scripts\python.exe` (and likewise for the other tools).

## Set up (once)

All Python packages go into the project's own `.venv`, never into the global Python.

```powershell
# 1. Get the code and create the virtual environment
git clone <repository-url> customs-copilot
cd customs-copilot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip

# 2. Install: CPU-only PyTorch first (the default wheels bundle CUDA), then the project
.\.venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python.exe -m pip install -e ".[dev,models]"

# 3. Configuration
Copy-Item .env.example .env
```

Edit `.env` and set:

| Key | Value |
|---|---|
| `POSTGRES_PASSWORD` | a new password of your choice for the app's database role (`customs`) |
| `REVIEWERS` | the people who may approve rule changes and filings, e.g. `Your Name <you@example.com>` (separate several with `;`) |
| `GROQ_API_KEY` | optional — your Groq key |

Leave every other key empty to use the defaults (they are listed and explained in
`.env.example`).

```powershell
# 4. Create the database role, the app database and the test database
#    (psql is found automatically; it asks for the postgres superuser password)
.\.venv\Scripts\python.exe -m scripts.setup_local_db

# 5. Create the tables
.\.venv\Scripts\alembic.exe upgrade head

# 6. Load the demo data — the JNPT case study: 4 shipments and their documents, port tariffs,
#    FX rates, the customs bulletin and the approved rulebook (and its search index).
#    The first run downloads the retrieval models (~525 MB). Safe to re-run.
.\.venv\Scripts\python.exe -m scripts.seed
```

A warning that you are "sending unauthenticated requests to the HF Hub" is harmless.

## Run

```powershell
.\.venv\Scripts\streamlit.exe run dashboards/Home.py
```

Open <http://localhost:8501> (the browser is not opened for you — settings in
`.streamlit/config.toml`). Stop with `Ctrl+C`. Times are shown in IST; everything is stored in
UTC.

| Page | What it is for |
|---|---|
| **Overview** | The front page: where the shipments are, what waits on a person, what the agents saved |
| **Mission Control** | Analyse every shipment, follow the audit trail live, before/after savings (ROI) |
| Shipping Ops | Shipments, documents, status; analyse one shipment; supervised clearance runs |
| Demurrage | Port tariffs (free days, daily rate) per container type and period |
| FX Rates | Spot, locked and gazetted exchange rates; resolve a rate for a lifecycle stage |
| Compliance | Review pending rule changes, the rulebook in force, ask the rulebook (with citations) |
| Customs Bulletin | The simulated external bulletin; run the regulatory watch scan |

The dashboards open in a light theme; the *Dark mode* toggle in the sidebar switches every
page to dark for the session. The visual system is written down in [`DESIGN.md`](DESIGN.md).

Then follow [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md). To learn the whole desk by doing,
seed the fifteen walkthrough cases (each stops a shipment a different way) and follow
[`docs/WALKTHROUGH.md`](docs/WALKTHROUGH.md):

```powershell
.\.venv\Scripts\python.exe -m scripts.seed_walkthrough
```

### Optional: real email and the scheduled watch

- Real email: follow [`docs/setup_gmail.md`](docs/setup_gmail.md), then set
  `EMAIL_BACKEND=gmail` and `REVIEWER_EMAIL` in `.env`.
- The regulatory watch runs on demand from Customs Bulletin → *Scans and changes* → **Run
  scheduled scan now**, or on a schedule in its own terminal:

  ```powershell
  .\.venv\Scripts\python.exe -m scheduler.watch_scheduler          # every WATCH_SCAN_INTERVAL_MINUTES (15)
  .\.venv\Scripts\python.exe -m scheduler.watch_scheduler --once   # one scan now
  ```

## Command-line tools

Everything the dashboards do is also available as a script (JSON output):

```powershell
.\.venv\Scripts\python.exe -m scripts.demo_jnpt --as-of 2026-09-28    # the whole case study + ROI
.\.venv\Scripts\python.exe -m scripts.verify_documents --all          # document verification agent
.\.venv\Scripts\python.exe -m scripts.analyse_shipments --all         # the full analysis pipeline
.\.venv\Scripts\python.exe -m scripts.clearance start --container MSKU7001001   # supervised run
.\.venv\Scripts\python.exe -m scripts.clearance --help                # list/show/resume/approve/...
.\.venv\Scripts\python.exe -m scripts.roi --help                      # ROI snapshots + compare
.\.venv\Scripts\python.exe -m scripts.index_rulebook                  # rebuild the search index
```

The six MCP servers start automatically when an agent needs them; to inspect one by hand:
`.\.venv\Scripts\python.exe -m mcp_servers.fx_rates_mcp.server` (stdio).

## Start over

To return to the original demo data (this deletes the app database and everything in it —
you are asked to type its name):

```powershell
.\.venv\Scripts\python.exe -m scripts.setup_local_db --reset
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m scripts.seed
.\.venv\Scripts\python.exe -m scripts.index_rulebook    # drop search entries of deleted rules
```

## Quality checks

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\black.exe --check .
.\.venv\Scripts\mypy.exe .
.\.venv\Scripts\python.exe -m scripts.scan_secrets   # nothing secret tracked by git
.\.venv\Scripts\pytest.exe                           # everything (~4 min); DB tests use POSTGRES_TEST_DB
.\.venv\Scripts\pytest.exe -m "not db and not models"   # no database, no model download
```

The test database is wiped and rebuilt on every run; tests never call an LLM or send email.
Tests marked `models` load the real retrieval models.

## Troubleshooting

- **`psql not found`** — pass its path: `python -m scripts.setup_local_db --psql "C:\Program
  Files\PostgreSQL\18\bin\psql.exe"`.
- **`password authentication failed for user "customs"`** — `POSTGRES_PASSWORD` in `.env`
  changed after setup; re-run `scripts.setup_local_db` (it re-syncs the role's password).
- **Port 8501 in use** — another Streamlit is running; stop it or add `--server.port 8502`.
- **Analyses show "data gaps" for FX** — the demo FX rates cover 2026-07-01 to 2026-10-31. Use
  an analysis date in that window (the dashboards default to today); the demo uses 2026-09-28.
- **A clearance run is stuck in `running`** (the process was closed mid-run) — Shipping Ops →
  the shipment → *Clearance* → *Abandon this run*, then start a new one.

## Project layout

```
config/        settings (.env)          models/      ORM + Pydantic records
services/      the single service layer (dashboards, MCP servers and agents all use it)
mcp_servers/   six MCP servers          agents/      LangGraph agents + the supervisor
rag/           retrieval (BM25 + BGE + rerank)   roi/  the ROI calculator
scheduler/     regulatory watch job     integrations/  email (outbox, Gmail)
dashboards/    Streamlit views          scripts/     setup, seed and CLI tools
alembic/       migrations (0001..)      tests/       pytest suite      docs/  specs
```

New migrations are hand-written and prefixed with the phase that introduced them
(`alembic revision --rev-id 0010 -m "<slug>"`), then checked with `alembic check`.
