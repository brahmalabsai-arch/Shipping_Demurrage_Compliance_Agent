---
version: 1
slug: "dashboards-home-py"
primary_target: "dashboards/Home.py"
related_targets: ["dashboards/views/1_Shipping_Ops.py","dashboards/views/2_Demurrage.py","dashboards/views/3_FX_Rates.py","dashboards/views/4_Compliance.py","dashboards/views/5_Customs_Bulletin.py","dashboards/views/6_Mission_Control.py"]
---

Scope: every dashboard page (Overview/Home, Mission Control, Shipping Ops, Demurrage, FX Rates, Compliance, Customs Bulletin). Mode: Operate. Audience: the demo audience of the JNPT walkthrough first, operators second. Constraints: Streamlit 1.64; all functions, data and widget keys preserved; plain operator language with technical ids in details; light default, dark via a sidebar toggle that must restyle every surface; fonts served locally.

## Direction contract

THESIS: The desk reads like Muselogist's calm logistics brochure turned into a working tool: pale-blue ground, ink type, sky-blue fields, and whatever needs a person sits on an ink-outlined panel with a sky-blue block offset behind it. It refuses the grey admin panel with one accent and pages made of raw data grids.

OWN-WORLD: Light: ground #f4f7ff, white panels with ink hairlines, sky #89bcff and pale #deecff fields, ink #0e0f18, coral only for blockers. Dark (sidebar toggle): ground #0d1018, panels #151a26, ink #eef2fb, the same sky. Young Serif for page titles and big figures, Outfit for everything else; square panels and buttons, pill status chips that always carry a word.

STORY: The viewer sees what is waiting on a person and what the agents saved, then follows one shipment: documents checked, stay priced, rulebook asked, a person files. Every number sits next to its basis.

FIRST VIEWPORT (Overview): sidebar with the wordmark, page groups and the dark-mode toggle. Main column: serif title and a one-line lede; beside it the offset-block panel "Waiting on a person" (pending rule reviews, clearance runs at a gate) with its action; below, the lifecycle route (quoted to closed) with the count at each stop; then the four-step path to the pages that do each step. Primary action: open Mission Control.

FORM: user-pinned direction (Muselogist reference), which overrides the dealt roll; seed key 278e8ecf. Signature move: the offset block, used only on what needs a person and on primary actions. Product move: the shipment lifecycle drawn as a route line.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
