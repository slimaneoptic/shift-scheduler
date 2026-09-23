"""AI shift scheduler demo: a one-week roster you can change in plain language."""
from __future__ import annotations

import html
import os

import pandas as pd
import streamlit as st

# Streamlit Cloud: copy settings from secrets into the environment before the parser loads.
try:
    for name in ("OPENROUTER_API_KEY", "OPENROUTER_MODELS"):
        if name in st.secrets:
            os.environ.setdefault(name, st.secrets[name])
except Exception:  # no secrets file locally
    pass

from model import DAYS, SHIFTS, WEEKEND, Roster, default_rules, diff, solve  # noqa: E402
from nl_parser import ParseError, ai_available, parse  # noqa: E402

# ---- edit these two lines -------------------------------------------------
AUTHOR = "Slimane"
HIRE_URL = "https://www.upwork.com/freelancers/~01d6ae0f1a06a43ac0"
# ---------------------------------------------------------------------------

MAX_AI_NOTES = 15  # per visitor session, keeps a public demo's API bill small
SHIFT_COLORS = {"M": "#2a78d6", "E": "#eb6834", "N": "#1baf7a"}  # validated palette, slots 1-3
GOOD, WARNING, CRITICAL = "#0ca30c", "#fab219", "#d03b3b"
EXAMPLES = ["Ahmed is off Thursday", "Sara can't do nights", "Need 4 on Friday evening", "Nadia prefers mornings", "Maria: max 3 shifts"]

st.set_page_config(page_title="AI shift scheduler", page_icon="🗓️", layout="wide")
st.markdown(
    """<style>
    .block-container {padding-top: 2rem; max-width: 1280px;}
    [data-testid="stMetricValue"] {font-variant-numeric: tabular-nums;}
    .grid-wrap {overflow-x: auto;}
    table.roster {border-collapse: separate; border-spacing: 3px; width: 100%; font-variant-numeric: tabular-nums; min-width: 760px;}
    table.roster th {font-weight: 600; font-size: .85rem; color: #4B5866; text-align: center; padding: .3rem 0;}
    table.roster th.we {background: #E6EAEE; border-radius: 4px;}
    table.roster td {height: 34px; border-radius: 4px; font-size: .85rem; text-align: center;}
    table.roster td.name {text-align: left; font-weight: 600; white-space: nowrap; padding-right: .6rem;}
    table.roster td.name small {font-weight: 400; color: #4B5866; margin-left: .35rem;}
    table.roster td.off {color: #8A96A3; background: #F0F2F4;}
    table.roster td.changed {box-shadow: inset 0 0 0 2px #14202B;}
    table.roster td.tot {color: #4B5866; font-size: .82rem; white-space: nowrap;}
    table.roster tr.cov td {height: 28px; font-size: .8rem; background: transparent;}
    table.roster tr.cov td.name {font-weight: 500; color: #4B5866;}
    .legend {display:flex; gap:1.2rem; flex-wrap:wrap; font-size:.88rem; color:#4B5866; margin:.4rem 0 .8rem;}
    .legend span.sw {display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:.35rem; vertical-align:-1px;}
    .chip {display:inline-block; padding:.25rem .6rem; border-radius:999px; background:#E6EAEE; margin:0 .35rem .35rem 0; font-size:.88rem;}
    .footer {margin-top:2.5rem; padding-top:1rem; border-top:1px solid #D5DBE1; color:#4B5866; font-size:.92rem;}
    </style>""",
    unsafe_allow_html=True,
)

NAMES = [e.name for e in default_rules().staff]


def build_rules(changes):
    rules = default_rules()
    for c in changes:
        c.apply(rules)
    return rules


def resolve(previous: Roster | None):
    rules = build_rules(st.session_state.changes)
    roster = solve(rules, 5, previous=previous)
    st.session_state.moved = diff(previous, roster, NAMES) if previous else []
    st.session_state.roster = roster
    st.session_state.rules = rules


if "roster" not in st.session_state:
    st.session_state.changes = []
    st.session_state.ai_calls = 0
    st.session_state.feedback = None
    resolve(None)


def apply_note(note: str):
    note = note.strip()
    if not note:
        return
    if ai_available() and st.session_state.ai_calls >= MAX_AI_NOTES:
        st.session_state.feedback = ("error", f"This demo reads up to {MAX_AI_NOTES} notes per visit. Reload the page to start over.")
        return
    try:
        if ai_available():
            st.session_state.ai_calls += 1
        result = parse(note, NAMES)
    except ParseError as err:
        st.session_state.feedback = ("error", str(err))
        return
    if not result.changes:
        hint = "" if result.engine == "ai" else " The basic reader was used, so try the phrasing in the examples."
        st.session_state.feedback = ("warning", f"Couldn't turn that into a roster change.{hint}")
        return
    st.session_state.changes = st.session_state.changes + [c for c in result.changes if c not in st.session_state.changes]
    resolve(st.session_state.roster)
    skipped = f" Skipped: {'; '.join(result.not_understood)}." if result.not_understood else ""
    busy = " (The AI was busy, so the basic reader handled this one.)" if result.engine == "rules (AI busy)" else ""
    st.session_state.feedback = ("success", "Understood: " + "; ".join(c.describe() for c in result.changes) + "." + skipped + busy)


def on_submit():
    st.session_state.pending_note = st.session_state.note
    st.session_state.note = ""


def on_example(text: str):
    st.session_state.pending_note = text


def remove_change(i: int):
    changes = list(st.session_state.changes)
    changes.pop(i)
    st.session_state.changes = changes
    resolve(st.session_state.roster)
    st.session_state.feedback = None


def reset():
    st.session_state.changes = []
    st.session_state.feedback = None
    resolve(None)


# ---------------------------------------------------------------- header + input
st.title("AI shift scheduler")
st.markdown(
    "A one-week roster for a 12-person clinic team, built by an optimizer that respects rest times, contracts and seniority. "
    "Tell it what changed in plain words. It re-plans and moves as few shifts as possible."
)

with st.form("note_form", clear_on_submit=False, border=False):
    c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
    c1.text_input("What changed?", key="note", placeholder="e.g. Omar is sick on Tuesday and Wednesday")
    c2.form_submit_button("Update roster", type="primary", width="stretch", on_click=on_submit)

cols = st.columns(len(EXAMPLES))
for col, text in zip(cols, EXAMPLES):
    col.button(text, key=f"ex_{text}", on_click=on_example, args=(text,), width="stretch")
st.caption(
    "Notes are read by an AI model, so write them the way you'd text a colleague." if ai_available()
    else "Notes are read by a basic English reader in this deployment. The AI version understands free-form notes."
)

if st.session_state.get("pending_note"):
    note = st.session_state.pop("pending_note")
    with st.spinner("Reading your note and re-planning…"):
        apply_note(note)

fb = st.session_state.feedback
if fb:
    getattr(st, fb[0])(fb[1])

roster: Roster = st.session_state.roster
rules = st.session_state.rules
moved = st.session_state.moved
changed_cells = {tuple(m.split(":")[0].rsplit(" ", 1)) for m in moved}

# ---------------------------------------------------------------- KPIs
total_slots = len(DAYS) * len(SHIFTS)
short_slots = {(d, s) for d, s, _ in roster.shortages}
nights = {e: sum(1 for d in DAYS if roster.assign.get((e, d)) == "N") for e in NAMES}
pref_ok = 0
for (e, s), sign in rules.prefer.items():
    worked = [roster.assign[(e, d)] for d in DAYS if (e, d) in roster.assign]
    on = sum(1 for w in worked if w == s)
    pref_ok += (on == 0) if sign < 0 else (bool(worked) and on * 2 >= len(worked))

k1, k2, k3, k4 = st.columns(4)
k1.metric("Shifts fully staffed", f"{total_slots - len(short_slots)} / {total_slots}")
k2.metric("Senior on every shift", "Yes" if not roster.no_senior else f"{len(roster.no_senior)} gaps")
k3.metric("Nights per person", f"{min(nights.values())}–{max(nights.values())}")
k4.metric("Preferences met", f"{pref_ok} / {len(rules.prefer)}")

if moved:
    with st.expander(f"Last update moved {len(moved)} shift{'s' * (len(moved) != 1)} (outlined below)", expanded=False):
        st.markdown("\n".join(f"- {m}" for m in moved))

# ---------------------------------------------------------------- roster grid
st.markdown(
    '<div class="legend">'
    + "".join(f'<span><span class="sw" style="background:{SHIFT_COLORS[k]}"></span>{v[0]} {v[1]}</span>' for k, v in SHIFTS.items())
    + '<span><span class="sw" style="background:#F0F2F4;border:1px solid #C4CCD4"></span>Off</span>'
    + '<span><span class="sw" style="box-shadow:inset 0 0 0 2px #14202B"></span>Changed by last note</span></div>',
    unsafe_allow_html=True,
)

by_name = {e.name: e for e in rules.staff}
head = "<tr><th></th>" + "".join(f'<th class="{"we" if d in WEEKEND else ""}">{d}</th>' for d in DAYS) + "<th>Shifts</th></tr>"
body = []
for e in NAMES:
    emp = by_name[e]
    tag = "Senior" if emp.senior else ""
    cells = [f'<td class="name">{e}<small>{tag}</small></td>']
    for d in DAYS:
        s = roster.assign.get((e, d))
        cls = "changed" if (e, d) in changed_cells else ""
        if s:
            c = SHIFT_COLORS[s]
            cells.append(f'<td class="{cls}" style="background:color-mix(in srgb,{c} 20%,#fff);border-left:4px solid {c}">{SHIFTS[s][0]}</td>')
        else:
            reason = "Leave" if (e, d) in rules.off else "Off"
            cells.append(f'<td class="off {cls}">{reason}</td>')
    worked = sum(1 for d in DAYS if (e, d) in roster.assign)
    cells.append(f'<td class="tot">{worked} of {emp.max_shifts}</td>')
    body.append("<tr>" + "".join(cells) + "</tr>")

cov = []
for s, (label, _) in SHIFTS.items():
    row = [f'<td class="name">{label} staff</td>']
    for d in DAYS:
        have = sum(1 for e in NAMES if roster.assign.get((e, d)) == s)
        need = rules.need.get((d, s), 0)
        if have < need:
            row.append(f'<td style="color:{CRITICAL};font-weight:700">⚠ {have}/{need}</td>')
        elif (d, s) in roster.no_senior:
            row.append(f'<td style="color:#8a6100;font-weight:600">▲ {have}/{need} no senior</td>')
        else:
            row.append(f'<td style="color:{GOOD}">✓ {have}/{need}</td>')
    row.append("<td></td>")
    cov.append('<tr class="cov">' + "".join(row) + "</tr>")

st.markdown(f'<div class="grid-wrap"><table class="roster">{head}{"".join(body)}{"".join(cov)}</table></div>', unsafe_allow_html=True)

grid = pd.DataFrame([[SHIFTS[roster.assign[(e, d)]][0] if (e, d) in roster.assign else "" for d in DAYS] for e in NAMES], index=NAMES, columns=DAYS)
st.download_button("Download roster (CSV)", grid.to_csv().encode(), "roster.csv", "text/csv")

# ---------------------------------------------------------------- applied changes
st.subheader("Changes applied this week")
if not st.session_state.changes:
    st.caption("None yet. Type a note above or click an example.")
else:
    for i, c in enumerate(st.session_state.changes):
        a, b = st.columns([6, 1], vertical_alignment="center")
        a.markdown(f'<span class="chip">{html.escape(c.describe())}</span>', unsafe_allow_html=True)
        b.button("Remove", key=f"rm_{i}", on_click=remove_change, args=(i,))
    st.button("Reset week", on_click=reset)

with st.expander("Rules the optimizer enforces"):
    st.markdown(
        f"""
**Always:** one shift per person per day · at least 11 hours of rest between shifts (no evening→morning or night→morning/evening) ·
at most 3 nights in a row · contract limits (full-time 4–5 shifts, part-time 2–3) · approved leave.

**As much as possible:** the required number of people on every shift and at least one senior on each ·
fair spread of nights and weekend shifts ({' & '.join(d for d in DAYS if d in WEEKEND)}) · personal preferences ·
as few changes as possible when you update the plan.

**Engine:** Google OR-Tools CP-SAT. This week solved in {roster.solve_s:.2f} s ({roster.status.lower()}).
Client versions add multiple wards, skills and certifications, monthly horizons, overtime costs, labor-law checks and Excel/HR-system import.
"""
    )

st.markdown(
    f'<div class="footer">Built by {AUTHOR}. I build scheduling and routing tools for operations teams: '
    f'your rules, your data, running in days. <a href="{HIRE_URL}" target="_blank">Work with me</a></div>',
    unsafe_allow_html=True,
)
