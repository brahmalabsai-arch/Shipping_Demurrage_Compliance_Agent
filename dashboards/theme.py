"""The dashboards' look: a calm logistics-brochure world (after Muselogist) in light and dark.

Light is the default; a toggle in the sidebar switches to dark for the session. Streamlit's
config theme (``.streamlit/config.toml``) styles its own widgets in light; this module injects
one stylesheet per run whose tokens follow the toggle and restyle every surface, Streamlit's
widgets included, so a dark page never shows a light island.

The world in short: pale-blue ground, white panels with ink hairlines, ink type, sky-blue
fields; Young Serif for titles and big figures, Outfit for everything else; square panels and
buttons, pill status chips that always carry a word. The signature is the *offset block*: what
needs a person (a waiting decision, a primary action) sits on an ink outline with a sky-blue
block offset behind it. Use it for that only.

Pages call :func:`setup_page` first, then compose with :func:`section`, :func:`chip`,
:func:`table`, :func:`route`, :func:`facts` and ``st.container(key=focus_key(...))``.
"""

from collections.abc import Iterable, Mapping, Sequence
from html import escape
from typing import Any, Literal

import streamlit as st
from streamlit.errors import StreamlitAPIException

from dashboards.ui import show_flash

Tone = Literal["ok", "warn", "stop", "info", "quiet"]

_DARK_STATE = "cc_dark"  # survives page switches (a widget's own key would not)
_DARK_WIDGET = "cc_dark_toggle"

LIGHT: dict[str, str] = {
    "ground": "#f4f7ff",
    "panel": "#ffffff",
    "panel-2": "#eaf0fd",
    "field": "#deecff",
    "sky": "#89bcff",
    "block": "#89bcff",
    "ink": "#0e0f18",
    "ink-2": "#3f4558",
    "ink-3": "#5c6378",
    "line": "rgba(14, 15, 24, 0.13)",
    "line-2": "rgba(14, 15, 24, 0.30)",
    "edge": "#0e0f18",
    "primary-bg": "#0e0f18",
    "primary-fg": "#ffffff",
    "input": "#ffffff",
    "code": "#edf2fd",
    "focus": "#0050bd",
    "select": "rgba(137, 188, 255, 0.45)",
    "ok-fg": "#1d6b4c",
    "ok-bg": "#e2f3ea",
    "warn-fg": "#7a5000",
    "warn-bg": "#fbefd2",
    "stop-fg": "#ad3324",
    "stop-bg": "#fde6e2",
    "info-fg": "#0b4ea8",
    "info-bg": "#deecff",
    "quiet-fg": "#3f4558",
    "quiet-bg": "#eaf0fd",
}
DARK: dict[str, str] = {
    "ground": "#0d1018",
    "panel": "#151a26",
    "panel-2": "#111521",
    "field": "#192640",
    "sky": "#89bcff",
    "block": "#3d6aa6",
    "ink": "#eef2fb",
    "ink-2": "#c3cadb",
    "ink-3": "#97a0b6",
    "line": "rgba(238, 242, 251, 0.12)",
    "line-2": "rgba(238, 242, 251, 0.28)",
    "edge": "#c9d3ea",
    "primary-bg": "#89bcff",
    "primary-fg": "#0b0e16",
    "input": "#0f131d",
    "code": "#101624",
    "focus": "#89bcff",
    "select": "rgba(137, 188, 255, 0.35)",
    "ok-fg": "#7fd8b0",
    "ok-bg": "#10261d",
    "warn-fg": "#f0c873",
    "warn-bg": "#2a2313",
    "stop-fg": "#ff9b8c",
    "stop-bg": "#2e1915",
    "info-fg": "#a9cbff",
    "info-bg": "#16233b",
    "quiet-fg": "#c3cadb",
    "quiet-bg": "#1b2130",
}

FONTS = """
@font-face { font-family: "Outfit"; src: url("app/static/fonts/Outfit-Variable.ttf")
  format("truetype"); font-weight: 100 900; font-display: swap; }
@font-face { font-family: "Young Serif"; src: url("app/static/fonts/YoungSerif-Regular.ttf")
  format("truetype"); font-weight: 400; font-display: swap; }
"""

STYLES = """
/* ---------------------------------------------------------------- ground, type, browser */
html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
  background: var(--ground) !important; color: var(--ink);
  font-family: "Outfit", system-ui, sans-serif; }
.stApp { font-feature-settings: "ss01"; }
::selection { background: var(--select); color: var(--ink); }
* { scrollbar-width: thin; scrollbar-color: var(--line-2) transparent; }
:focus-visible { outline: 2px solid var(--focus) !important; outline-offset: 2px; }
a, a:visited { color: var(--ink); text-decoration-color: var(--sky);
  text-underline-offset: 3px; text-decoration-thickness: 2px; }
a:hover { text-decoration-color: var(--ink); }
/* the top bar stays put while the page scrolls: a solid ground and a hairline keep the
   navigation readable over whatever passes beneath it */
[data-testid="stHeader"] { background: var(--ground) !important;
  border-bottom: 1px solid var(--line); box-shadow: none !important; }
[data-testid="stDecoration"] { display: none; }
[data-testid="stMainBlockContainer"] { padding-top: 5.5rem; padding-bottom: 5rem;
  max-width: 1240px; }
[data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li, [data-testid="stText"] { color: var(--ink); }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p,
[data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label { color: var(--ink-3) !important; }
[data-testid="stWidgetLabel"] p { font-weight: 500; color: var(--ink-2) !important; }
h1, h2, h3, h4 { font-family: "Young Serif", Georgia, serif !important; font-weight: 400 !important;
  color: var(--ink) !important; letter-spacing: -0.01em; }
code { font-variant-numeric: tabular-nums; }

/* ---------------------------------------------------------------- page header */
.cc-head { margin: 0 0 1.75rem; max-width: 62rem; }
.cc-head h1 { font-size: clamp(2.1rem, 3.4vw, 3rem); line-height: 1.08; margin: 0 0 .6rem;
  padding: 0; }
.cc-head p { font-size: 1.12rem; line-height: 1.55; color: var(--ink-2); margin: 0;
  max-width: 62ch; }
.cc-section { margin: 2.6rem 0 .9rem; }
.cc-section h3 { font-size: 1.45rem; line-height: 1.2; margin: 0 0 .25rem; padding: 0; }
.cc-section p { color: var(--ink-3); margin: 0; max-width: 70ch; line-height: 1.5; }

/* ---------------------------------------------------------------- top navigation */
[data-testid="stTopNavLinkContainer"] a, [data-testid="stTopNavLink"],
[data-testid="stTopNavLink"] span, [data-testid="stNavSectionHeader"] {
  color: var(--ink-2) !important; font-weight: 500; text-decoration: none; }
[data-testid="stTopNavLink"]:hover { background: var(--field) !important; }
[data-testid="stTopNavLink"][aria-current="page"], [data-testid="stTopNavLink"][aria-current="page"] span {
  color: var(--ink) !important; font-weight: 600; }
[data-testid="stTopNavLink"][aria-current="page"] { box-shadow: inset 0 -2px 0 var(--ink); }

/* ---------------------------------------------------------------- sidebar */
[data-testid="stSidebar"], [data-testid="stSidebar"] > div { background: var(--panel-2) !important;
  border-right: 1px solid var(--line); }
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p { color: var(--ink-2); }
.cc-brand { padding: .2rem 0 1.1rem; border-bottom: 1px solid var(--line); margin-bottom: .6rem; }
.cc-brand strong { display: block; font-family: "Young Serif", Georgia, serif; font-weight: 400;
  font-size: 1.35rem; line-height: 1.15; color: var(--ink); }
.cc-brand span { display: block; margin-top: .35rem; font-size: .86rem; color: var(--ink-3);
  line-height: 1.4; }
[data-testid="stSidebarNav"] a span { color: var(--ink-2) !important; }
[data-testid="stSidebarCollapseButton"] svg, [data-testid="stExpandSidebarButton"] svg,
[data-testid="stSidebarCollapsedControl"] svg { color: var(--ink-2) !important; fill: var(--ink-2); }

/* ---------------------------------------------------------------- inputs
   Streamlit 1.6x draws its fields with react-aria (``*RootElement``, a ``group`` inside select
   and multiselect, ``stDateInputField``); the BaseWeb selectors stay for older builds. */
[data-baseweb="input"], [data-baseweb="base-input"], [data-baseweb="textarea"],
[data-baseweb="select"] > div, [data-testid="stNumberInputContainer"],
[data-testid="stDateInputField"], [data-testid$="RootElement"],
[data-testid="stSelectbox"] [role="group"], [data-testid="stMultiSelect"] [role="group"],
[data-testid="stTimeInput"] [role="group"], [data-baseweb="input"] input, textarea, input {
  background: var(--input) !important; color: var(--ink) !important;
  -webkit-text-fill-color: var(--ink) !important; }
[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div,
[data-testid="stNumberInputContainer"], [data-testid="stDateInputField"],
[data-testid$="RootElement"], [data-testid="stSelectbox"] [role="group"],
[data-testid="stMultiSelect"] [role="group"], [data-testid="stTimeInput"] [role="group"] {
  border-color: var(--line-2) !important; }
[data-baseweb="input"]:focus-within, [data-baseweb="textarea"]:focus-within,
[data-baseweb="select"] > div:focus-within, [data-testid="stNumberInputContainer"]:focus-within,
[data-testid="stDateInputField"]:focus-within, [data-testid$="RootElement"]:focus-within,
[data-testid="stSelectbox"] [role="group"]:focus-within,
[data-testid="stMultiSelect"] [role="group"]:focus-within {
  border-color: var(--edge) !important; box-shadow: 0 0 0 2px var(--select) !important; }
[role="spinbutton"] { color: var(--ink) !important; }
[role="spinbutton"][data-placeholder] { color: var(--ink-3) !important; }
input::placeholder, textarea::placeholder { color: var(--ink-3) !important;
  -webkit-text-fill-color: var(--ink-3) !important; opacity: 1; }
[data-baseweb="select"] svg, [data-testid="stNumberInputContainer"] svg,
[data-testid="stSelectbox"] button, [data-testid="stMultiSelect"] button,
[data-testid="stSelectbox"] svg, [data-testid="stMultiSelect"] svg {
  color: var(--ink-2) !important; fill: var(--ink-2) !important; background: transparent !important; }
/* open lists and the calendar live in a portal, outside the page tree */
[data-testid$="VirtualDropdown"], [data-testid="stDateInputCalendar"], [role="listbox"] {
  background: var(--panel) !important; color: var(--ink) !important;
  border: 1px solid var(--line-2) !important; border-radius: 0 !important; }
[data-testid$="VirtualDropdown"] [role="option"] { color: var(--ink) !important; }
[data-testid$="VirtualDropdown"] [role="option"][data-focused],
[data-testid$="VirtualDropdown"] [role="option"]:hover { background: var(--field) !important; }
[data-testid="stDateInputCalendar"] * { color: var(--ink); }
[data-testid="stDateInputCalendar"] [role="button"]:hover { background: var(--field) !important; }
[data-testid="stDateInputCalendar"] [data-selected="true"] { background: var(--primary-bg) !important;
  color: var(--primary-fg) !important; }
[data-testid="stDateInputCalendar"] [data-disabled], [data-testid="stDateInputCalendar"] [data-outside-month] {
  color: var(--ink-3) !important; opacity: .55; }
[data-testid="stNumberInputContainer"] button { background: var(--panel-2) !important;
  color: var(--ink-2) !important; border-color: var(--line) !important; }
[data-testid="stNumberInputContainer"] button:hover { background: var(--field) !important; }
[data-baseweb="popover"] > div, [data-baseweb="popover"] ul, [data-baseweb="menu"],
[data-baseweb="calendar"], [data-baseweb="calendar"] > div, [data-baseweb="select"] [role="listbox"] {
  background: var(--panel) !important; color: var(--ink) !important; border-color: var(--line) !important; }
[data-baseweb="popover"] li, [role="option"] { color: var(--ink) !important; background: transparent; }
[data-baseweb="popover"] li:hover, [role="option"][aria-selected="true"], [role="option"]:hover {
  background: var(--field) !important; }
[data-baseweb="calendar"] div, [data-baseweb="calendar"] button { color: var(--ink) !important; }
[data-baseweb="calendar"] [aria-selected="true"] div, [data-baseweb="calendar"] [aria-selected="true"] {
  color: var(--primary-fg) !important; }
[data-baseweb="calendar"] [aria-selected="true"]::after { background: var(--primary-bg) !important;
  border-color: var(--primary-bg) !important; }
[data-baseweb="tag"] { background: var(--field) !important; color: var(--ink) !important; }
[data-testid="stCheckbox"] label, [data-testid="stToggle"] label, [data-testid="stRadio"] label,
[data-baseweb="checkbox"] div { color: var(--ink) !important; }
/* toggles: a track (a div holding only the thumb) that reads in both modes, on and off */
[data-testid="stCheckbox"] label > div:not([data-testid]):has(> div:only-child) {
  background: var(--line-2) !important; transition: background-color 160ms ease; }
[data-testid="stCheckbox"] label > div:not([data-testid]):has(> div:only-child) > div {
  background: var(--panel) !important; box-shadow: 0 0 0 1px var(--line); }
[data-testid="stCheckbox"] label:has(input:checked) > div:not([data-testid]):has(> div:only-child) {
  background: var(--primary-bg) !important; }
[data-testid="stCheckbox"] label:has(input:checked) > div:not([data-testid]):has(> div:only-child) > div {
  background: var(--primary-fg) !important; }

/* ---------------------------------------------------------------- buttons */
.stButton > button, [data-testid="stFormSubmitButton"] > button, [data-testid="stPageLink"] a,
[data-testid="stDownloadButton"] > button {
  border-radius: 0 !important; font-family: "Outfit", sans-serif; font-weight: 500;
  border: 1px solid var(--line-2) !important; background: var(--panel) !important;
  color: var(--ink) !important; min-height: 2.6rem;
  transition: background-color 160ms ease, border-color 160ms ease,
    transform 160ms cubic-bezier(0.23, 1, 0.32, 1), box-shadow 160ms cubic-bezier(0.23, 1, 0.32, 1); }
.stButton > button p, [data-testid="stFormSubmitButton"] > button p { color: inherit !important; }
@media (hover: hover) and (pointer: fine) {
  .stButton > button:hover, [data-testid="stFormSubmitButton"] > button:hover,
  [data-testid="stPageLink"] a:hover { border-color: var(--edge) !important;
    background: var(--field) !important; } }
.stButton > button:active, [data-testid="stFormSubmitButton"] > button:active {
  transform: scale(0.98); }
.stButton > button:disabled, [data-testid="stFormSubmitButton"] > button:disabled {
  opacity: .45; box-shadow: none !important; }
/* primary = the offset block: the button sits on a sky-blue block and sinks into it on hover */
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {
  background: var(--primary-bg) !important; color: var(--primary-fg) !important;
  border: 1px solid var(--edge) !important; box-shadow: -4px 4px 0 0 var(--block) !important; }
@media (hover: hover) and (pointer: fine) {
  [data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primaryFormSubmit"]:hover {
    background: var(--primary-bg) !important; box-shadow: 0 0 0 0 var(--block) !important;
    transform: translate(-4px, 4px); } }
[data-testid="stBaseButton-primary"]:active, [data-testid="stBaseButton-primaryFormSubmit"]:active {
  box-shadow: 0 0 0 0 var(--block) !important; transform: translate(-4px, 4px) scale(0.98); }

/* ---------------------------------------------------------------- tabs, expanders, forms */
[data-baseweb="tab-list"] { gap: 1.6rem; border-bottom: 1px solid var(--line); background: transparent; }
[data-baseweb="tab"] { background: transparent !important; padding: .55rem 0 !important; }
[data-baseweb="tab"] p { color: var(--ink-3) !important; font-weight: 500; font-size: 1rem; }
[data-baseweb="tab"][aria-selected="true"] p { color: var(--ink) !important; }
[data-baseweb="tab-highlight"], [data-testid="stTab"] .react-aria-SelectionIndicator {
  background: var(--ink) !important; height: 2px !important; }
[role="tablist"] { border-bottom-color: var(--line) !important; }
[data-testid="stTab"] p { color: var(--ink-3) !important; font-weight: 500; }
[data-testid="stTab"][aria-selected="true"] p { color: var(--ink) !important; }
@media (hover: hover) and (pointer: fine) { [data-testid="stTab"]:hover p { color: var(--ink) !important; } }
[data-baseweb="tab-border"] { display: none; }
[data-testid="stExpander"] details { background: var(--panel) !important; border: 1px solid var(--line) !important;
  border-radius: 0 !important; }
[data-testid="stExpander"] summary, [data-testid="stExpander"] summary p { color: var(--ink-2) !important;
  font-weight: 500; }
[data-testid="stExpander"] summary:hover { background: var(--field); }
[data-testid="stExpander"] summary svg { color: var(--ink-3) !important; }
[data-testid="stExpanderDetails"] { border-color: var(--line) !important; }
[data-testid="stForm"] { background: var(--panel); border: 1px solid var(--line) !important;
  border-radius: 0 !important; padding: 1.4rem 1.5rem 1.2rem !important; }
[data-testid="stVerticalBlockBorderWrapper"] { border-color: var(--line) !important; border-radius: 0 !important; }
[class*="st-key-panel"] { background: var(--panel); border: 1px solid var(--line);
  padding: 1.25rem 1.4rem; }
/* the offset block for what needs a person */
[class*="st-key-focus"] { background: var(--panel); border: 1px solid var(--edge);
  box-shadow: -7px 7px 0 0 var(--block); padding: 1.35rem 1.5rem 1.4rem; margin: .4rem 0 1.4rem 7px; }
[class*="st-key-field"] { background: var(--field); padding: 1.35rem 1.5rem; border: 1px solid var(--line); }
/* a page link that is the primary action of its block */
[class*="st-key-cta"] [data-testid="stPageLink"] a { background: var(--primary-bg) !important;
  border: 1px solid var(--edge) !important; box-shadow: -4px 4px 0 0 var(--block);
  padding: .45rem 1.1rem; }
[class*="st-key-cta"] [data-testid="stPageLink"] a span, [class*="st-key-cta"] [data-testid="stPageLink"] a p {
  color: var(--primary-fg) !important; font-weight: 500; }
@media (hover: hover) and (pointer: fine) {
  [class*="st-key-cta"] [data-testid="stPageLink"] a:hover { background: var(--primary-bg) !important;
    box-shadow: 0 0 0 0 var(--block); transform: translate(-4px, 4px); } }
[data-testid="stPageLink"] a { padding: .35rem .8rem; text-decoration: none; }
[data-testid="stPageLink"] a span, [data-testid="stPageLink"] a p { color: var(--ink) !important; }

/* ---------------------------------------------------------------- toast: what an action did,
   shown in the corner so the page under it does not jump */
[data-testid="stToast"] { background: var(--panel) !important; color: var(--ink) !important;
  border: 1px solid var(--edge) !important; border-radius: 0 !important;
  box-shadow: -5px 5px 0 0 var(--block) !important; max-width: 30rem; }
[data-testid="stToast"] p, [data-testid="stToast"] [data-testid="stMarkdownContainer"] {
  color: var(--ink) !important; }
[data-testid="stToast"] [data-testid="stIconMaterial"] { color: var(--ok-fg) !important; }
[data-testid="stToast"] button svg { color: var(--ink-3) !important; }

/* ---------------------------------------------------------------- alerts as calm notes */
[data-testid="stAlertContainer"] { border-radius: 0 !important; border: 1px solid var(--line);
  color: var(--ink) !important; }
[data-testid="stAlertContainer"] p, [data-testid="stAlertContainer"] li { color: var(--ink) !important; }
[data-testid="stAlert"]:has([data-testid="stAlertContentSuccess"]) [data-testid="stAlertContainer"] {
  background: var(--ok-bg) !important; border-color: color-mix(in srgb, var(--ok-fg) 35%, transparent); }
[data-testid="stAlert"]:has([data-testid="stAlertContentInfo"]) [data-testid="stAlertContainer"] {
  background: var(--info-bg) !important; border-color: color-mix(in srgb, var(--info-fg) 30%, transparent); }
[data-testid="stAlert"]:has([data-testid="stAlertContentWarning"]) [data-testid="stAlertContainer"] {
  background: var(--warn-bg) !important; border-color: color-mix(in srgb, var(--warn-fg) 35%, transparent); }
[data-testid="stAlert"]:has([data-testid="stAlertContentError"]) [data-testid="stAlertContainer"] {
  background: var(--stop-bg) !important; border-color: color-mix(in srgb, var(--stop-fg) 40%, transparent); }
[data-testid="stAlertContentSuccess"] svg, [data-testid="stAlertContentSuccess"] [data-testid="stIconMaterial"] { color: var(--ok-fg) !important; }
[data-testid="stAlertContentInfo"] svg, [data-testid="stAlertContentInfo"] [data-testid="stIconMaterial"] { color: var(--info-fg) !important; }
[data-testid="stAlertContentWarning"] svg, [data-testid="stAlertContentWarning"] [data-testid="stIconMaterial"] { color: var(--warn-fg) !important; }
[data-testid="stAlertContentError"] svg, [data-testid="stAlertContentError"] [data-testid="stIconMaterial"] { color: var(--stop-fg) !important; }

/* ---------------------------------------------------------------- figures */
[data-testid="stMetric"] { background: transparent; padding: .2rem 0; }
[data-testid="stMetricLabel"] p { color: var(--ink-3) !important; font-weight: 500; font-size: .92rem; }
[data-testid="stMetricValue"] { font-family: "Young Serif", Georgia, serif; color: var(--ink) !important;
  font-size: 2rem !important; font-variant-numeric: lining-nums tabular-nums; }
[data-testid="stMetricValue"] div { color: var(--ink) !important; }

/* ---------------------------------------------------------------- tables */
[data-testid="stTable"] { background: var(--panel); border: 1px solid var(--line); overflow-x: auto; }
[data-testid="stTable"] table { color: var(--ink); background: var(--panel); border-collapse: collapse;
  width: 100%; font-variant-numeric: tabular-nums; }
[data-testid="stTable"] th { background: var(--panel-2) !important; color: var(--ink-2) !important;
  font-weight: 600; font-size: .86rem; text-align: left; border-color: var(--line) !important;
  padding: .55rem .8rem !important; white-space: nowrap; }
[data-testid="stTable"] td { color: var(--ink) !important; border-color: var(--line) !important;
  padding: .6rem .8rem !important; vertical-align: top; line-height: 1.45; font-size: .95rem; }
[data-testid="stTable"] tr:hover td { background: color-mix(in srgb, var(--field) 55%, transparent); }

/* ---------------------------------------------------------------- code, json, misc */
[data-testid="stCode"] pre, [data-testid="stCode"] code, pre, code {
  background: var(--code) !important; color: var(--ink) !important; border-radius: 0 !important; }
[data-testid="stCode"] pre { border: 1px solid var(--line); }
[data-testid="stCode"] div { background-color: transparent !important; }
[data-testid="stCode"] span { color: inherit; }
[data-testid="stCode"] .token.inserted, [data-testid="stCode"] .token.deleted { font-weight: 600; }
[data-testid="stCode"] .token.inserted { color: var(--ok-fg) !important; }
[data-testid="stCode"] .token.deleted { color: var(--stop-fg) !important; }
[data-testid="stProgress"] > div > div { background: var(--panel-2) !important; }
[data-testid="stProgress"] > div > div > div > div { background: var(--ink) !important; }
[data-testid="stSpinner"] i { border-color: var(--line-2) var(--line-2) var(--line-2) var(--ink) !important; }
[data-testid="stTooltipContent"], [data-baseweb="tooltip"] > div { background: var(--panel) !important;
  color: var(--ink) !important; border: 1px solid var(--line); }
hr { border-color: var(--line) !important; }

/* ---------------------------------------------------------------- shared components */
.cc-chip { display: inline-flex; align-items: center; gap: .35rem; padding: .12rem .62rem;
  border-radius: 999px; font-size: .8rem; font-weight: 500; line-height: 1.5; white-space: nowrap;
  border: 1px solid color-mix(in srgb, currentColor 28%, transparent); }
.cc-chip.ok { color: var(--ok-fg); background: var(--ok-bg); }
.cc-chip.warn { color: var(--warn-fg); background: var(--warn-bg); }
.cc-chip.stop { color: var(--stop-fg); background: var(--stop-bg); }
.cc-chip.info { color: var(--info-fg); background: var(--info-bg); }
.cc-chip.quiet { color: var(--quiet-fg); background: var(--quiet-bg); }
.cc-facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
  gap: 1rem 1.6rem; margin: .2rem 0 1rem; }
.cc-facts div { border-top: 1px solid var(--line); padding-top: .55rem; }
.cc-facts dt { color: var(--ink-3); font-size: .84rem; margin: 0 0 .15rem; }
.cc-facts dd { margin: 0; color: var(--ink); font-size: 1.02rem; font-weight: 500;
  font-variant-numeric: tabular-nums; }
.cc-figure { font-family: "Young Serif", Georgia, serif; font-size: clamp(2.2rem, 4vw, 3.2rem);
  line-height: 1.05; color: var(--ink); font-variant-numeric: lining-nums tabular-nums;
  margin: .1rem 0 .35rem; }
.cc-note { color: var(--ink-3); font-size: .93rem; line-height: 1.5; margin: .25rem 0 1.1rem; }
.cc-lead { color: var(--ink-2); font-size: 1.05rem; line-height: 1.55; margin: 0 0 .8rem; max-width: 66ch; }
.cc-route { display: grid; grid-template-columns: repeat(var(--stops), minmax(0, 1fr)); margin: .6rem 0 1.4rem;
  position: relative; }
.cc-route::before { content: ""; position: absolute; left: 0; right: 0; top: 7px; height: 1px;
  background: var(--line-2); }
.cc-stop { position: relative; padding-top: 1.35rem; padding-right: .6rem; }
.cc-stop::before { content: ""; position: absolute; top: 0; left: 0; width: 13px; height: 13px;
  border-radius: 50%; background: var(--ground); border: 1px solid var(--line-2); box-sizing: border-box; }
.cc-stop.passed::before { background: var(--sky); border-color: var(--sky); }
.cc-stop.here::before { background: var(--ink); border-color: var(--ink);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--sky) 55%, transparent); }
.cc-stop b { display: block; font-weight: 500; color: var(--ink-2); font-size: .92rem; line-height: 1.3; }
.cc-stop.here b { color: var(--ink); font-weight: 600; }
.cc-stop span { display: block; font-family: "Young Serif", Georgia, serif; font-size: 1.6rem;
  line-height: 1.1; color: var(--ink); margin-bottom: .2rem; font-variant-numeric: lining-nums tabular-nums; }
.cc-stop span.zero { color: var(--ink-3); }
.cc-step { border-top: 1px solid var(--line-2); padding-top: .7rem; }
.cc-step b { display: block; font-size: 1.02rem; color: var(--ink); margin-bottom: .2rem; font-weight: 600; }
.cc-step p { color: var(--ink-3); margin: 0; line-height: 1.5; font-size: .95rem; }
@media (max-width: 760px) {
  [data-testid="stMainBlockContainer"] { padding-top: 3.5rem; }
  .cc-route { grid-template-columns: 1fr 1fr; row-gap: 1rem; }
  .cc-route::before { display: none; }
  [class*="st-key-focus"] { margin-left: 7px; } }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { transition: none !important; animation-duration: 1ms !important; } }
"""


def is_dark() -> bool:
    """The session's theme: dark if the sidebar toggle (or ``?theme=dark``) chose it."""
    if _DARK_STATE not in st.session_state:
        st.session_state[_DARK_STATE] = st.query_params.get("theme") == "dark"
    return bool(st.session_state[_DARK_STATE])


def _toggle_changed() -> None:
    st.session_state[_DARK_STATE] = bool(st.session_state[_DARK_WIDGET])


def _css(tokens: Mapping[str, str]) -> str:
    variables = "".join(f"--{name}: {value};" for name, value in tokens.items())
    scheme = "dark" if tokens is DARK else "light"
    return f"<style>{FONTS}:root, .stApp {{{variables} color-scheme: {scheme};}}{STYLES}</style>"


def setup_page(title: str, lede: str, *, tab_title: str | None = None, icon: str) -> None:
    """Page config, the theme for this session, the sidebar brand + dark-mode toggle, the page
    header and any flash message from the previous action. Call once, first, on every page."""
    st.set_page_config(page_title=f"{tab_title or title} · Clearance Copilot", page_icon=icon,
                       layout="wide")  # fmt: skip
    dark = is_dark()
    st.html(_css(DARK if dark else LIGHT))
    st.sidebar.markdown(
        '<div class="cc-brand"><strong>Clearance Copilot</strong>'
        "<span>Nhava Sheva import desk. All shipments, rates and rules here are simulated "
        "demo data.</span></div>",
        unsafe_allow_html=True,
    )
    st.sidebar.toggle("Dark mode", value=dark, key=_DARK_WIDGET, on_change=_toggle_changed)
    st.markdown(
        f'<div class="cc-head"><h1>{escape(title)}</h1><p>{escape(lede)}</p></div>',
        unsafe_allow_html=True,
    )
    show_flash()


def section(title: str, note: str | None = None) -> None:
    """A section heading, with one plain sentence on what the section is for."""
    body = f"<p>{escape(note)}</p>" if note else ""
    st.markdown(f'<div class="cc-section"><h3>{escape(title)}</h3>{body}</div>',
                unsafe_allow_html=True)  # fmt: skip


def chip(text: str, tone: Tone = "quiet") -> str:
    """A status word on a pill (HTML) — the word is the status; colour only supports it."""
    return f'<span class="cc-chip {tone}">{escape(text)}</span>'


def chips(items: Iterable[tuple[str, Tone]]) -> None:
    """A row of status chips."""
    st.markdown(" ".join(chip(text, tone) for text, tone in items), unsafe_allow_html=True)


def facts(pairs: Sequence[tuple[str, str]]) -> None:
    """Key facts as a quiet label/value grid."""
    cells = "".join(f"<div><dt>{escape(k)}</dt><dd>{escape(v)}</dd></div>" for k, v in pairs)
    st.markdown(f'<dl class="cc-facts">{cells}</dl>', unsafe_allow_html=True)


def figure(value: str, note: str | None = None) -> None:
    """One large figure (Young Serif) with its basis underneath."""
    body = f'<p class="cc-note">{escape(note)}</p>' if note else ""
    st.markdown(f'<div class="cc-figure">{escape(value)}</div>{body}', unsafe_allow_html=True)


def lead(text: str) -> None:
    """A short plain-language paragraph introducing what follows."""
    st.markdown(f'<p class="cc-lead">{escape(text)}</p>', unsafe_allow_html=True)


def route(stops: Sequence[tuple[str, str | None]], *, here: int | None = None) -> None:
    """The shipment lifecycle drawn as a route: one stop per stage, an optional figure under
    each, stops before ``here`` marked as passed and ``here`` as the current stop."""
    cells = []
    for i, (label, figure_text) in enumerate(stops):
        state = "" if here is None else ("passed" if i < here else "here" if i == here else "")
        value = ""
        if figure_text is not None:
            zero = " zero" if figure_text in ("0", "") else ""
            value = f'<span class="{zero.strip()}">{escape(figure_text)}</span>'
        cells.append(f'<div class="cc-stop {state}">{value}<b>{escape(label)}</b></div>')
    st.markdown(f'<div class="cc-route" style="--stops:{len(stops)}">{"".join(cells)}</div>',
                unsafe_allow_html=True)  # fmt: skip


def steps(items: Sequence[tuple[str, str]]) -> None:
    """A short numbered-free sequence of what happens, in columns."""
    for col, (title, text) in zip(st.columns(len(items)), items, strict=True):
        col.markdown(f'<div class="cc-step"><b>{escape(title)}</b><p>{escape(text)}</p></div>',
                     unsafe_allow_html=True)  # fmt: skip


def table(rows: Sequence[Mapping[str, Any]], *, empty: str | None = None) -> None:
    """Rows as a themed table (themeable in both modes, unlike the canvas data grid)."""
    if rows:
        st.table(list(rows), hide_index=True, border="horizontal")
    elif empty:
        st.info(empty)


def tabs(labels: Sequence[str], *, key: str) -> Sequence[Any]:
    """Tabs that stay on the open tab when the page reruns after an action (plain tabs jump
    back to the first). A label carrying a count, e.g. "Waiting for review (2)", keeps its
    place when the count changes.

    Streamlit identifies the tabs by their labels *and* their ``default``: change either and it
    starts a new widget, dropping a click made on the old one (the next click would open the
    previous tab instead). So the default is changed only when the labels change anyway — then
    it is the open tab under its new label ("Waiting for review (1)" for "... (2)") — and kept
    as it was on every other run."""
    labels = list(labels)
    identity = f"_{key}_identity"  # (labels, default) of the last run
    open_stem = f"_{key}_open"  # the open tab after the last run, without its count
    last: tuple[tuple[str, ...], str] | None = st.session_state.get(identity)
    if last is None:
        default = labels[0]
    elif tuple(labels) == last[0]:
        default = last[1]
    else:
        stem = st.session_state.get(open_stem, _stem(last[1]))
        default = next((label for label in labels if _stem(label) == stem), labels[0])
    containers = st.tabs(labels, key=key, default=default, on_change="rerun")
    st.session_state[identity] = (tuple(labels), default)
    if (current := st.session_state.get(key)) is not None:  # reliable once the tabs exist
        st.session_state[open_stem] = _stem(str(current))
    return containers


def _stem(label: str) -> str:
    """A tab label without its trailing count: "Waiting for review (2)" -> "Waiting for review"."""
    return label.split(" (")[0]


def expander(label: str, *, key: str, expanded: bool = False) -> Any:
    """An expander that stays open across the rerun that follows an action inside it."""
    return st.expander(label, expanded=expanded, key=key, on_change="rerun")


def link(page: str, label: str, icon: str | None = None) -> None:
    """A link to another page (a plain caption when the page runs outside the app's
    navigation, e.g. under the test harness)."""
    try:
        st.page_link(page, label=label, icon=icon)
    except StreamlitAPIException:
        st.caption(label)


def focus_key(name: str) -> str:
    """Container key that gives a block the offset-block treatment (what needs a person)."""
    return f"focus_{name}"


def panel_key(name: str) -> str:
    """Container key for a plain white panel."""
    return f"panel_{name}"


def field_key(name: str) -> str:
    """Container key for a pale-blue field (a highlighted, informational region)."""
    return f"field_{name}"
