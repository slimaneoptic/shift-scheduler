"""Staff rostering with OR-Tools CP-SAT.

Hard rules: one shift per day, rest time between shifts, contract limits, max nights in a row,
approved time off. Coverage and seniority are soft with a very high penalty, so the solver
always returns a roster and reports exactly where it falls short. An empty shift costs far more
than a short one, so shortages are spread instead of leaving a shift with nobody.
Soft goals: respect preferences, spread nights and weekends fairly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import pandas as pd
from ortools.sat.python import cp_model

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKEND = {"Fri", "Sat"}  # Gulf weekend by default; change for other regions
SHIFTS = {"M": ("Morning", "07:00–15:00"), "E": ("Evening", "15:00–23:00"), "N": ("Night", "23:00–07:00")}
# pairs (today, tomorrow) that leave less than 11 hours of rest
FORBIDDEN_NEXT = {("E", "M"), ("N", "M"), ("N", "E")}

SHORTAGE_PENALTY = 10_000
NO_SENIOR_PENALTY = 5_000
EMPTY_SHIFT_PENALTY = 30_000  # a shift with nobody at all is far worse than one short
PREF_PENALTY = 40
FAIRNESS_WEIGHT = 60
CHANGE_PENALTY = 50  # per shift moved vs the previous roster (minimal disruption)


@dataclass
class Employee:
    name: str
    senior: bool
    max_shifts: int  # per week
    min_shifts: int = 0


@dataclass
class Rules:
    staff: list[Employee]
    need: dict[tuple[str, str], int]  # (day, shift) -> people required
    off: set[tuple[str, str]] = field(default_factory=set)  # (employee, day)
    blocked: set[tuple[str, str, str]] = field(default_factory=set)  # (employee, day, shift)
    prefer: dict[tuple[str, str], int] = field(default_factory=dict)  # (employee, shift) -> +1 likes / -1 avoids
    max_nights_in_row: int = 3


@dataclass
class Roster:
    status: str
    assign: dict[tuple[str, str], str]  # (employee, day) -> shift code
    shortages: list[tuple[str, str, int]]  # (day, shift, missing)
    no_senior: list[tuple[str, str]]
    solve_s: float
    objective: float


SAMPLE_PREFS = {("Nadia", "M"): 1, ("Priya", "N"): -1, ("John", "N"): 1}


def sample_team() -> pd.DataFrame:
    rows = [
        ("Ahmed", True, 4, 5), ("Sara", True, 4, 5), ("Omar", False, 4, 5), ("Fatima", False, 4, 5),
        ("Youssef", True, 4, 5), ("Layla", False, 4, 5), ("John", False, 4, 5), ("Maria", True, 4, 5),
        ("Khalid", True, 4, 5), ("Nadia", False, 2, 3), ("Priya", False, 4, 5), ("Hassan", False, 2, 3),
    ]
    return pd.DataFrame(rows, columns=["name", "senior", "min shifts", "max shifts"])


def sample_need() -> pd.DataFrame:
    need = {d: [2, 2, 2] if d in WEEKEND else [3, 3, 2] for d in DAYS}
    return pd.DataFrame(need, index=[SHIFTS[s][0] for s in SHIFTS])


def build_rules(team: pd.DataFrame, need: pd.DataFrame, prefer: dict | None = None) -> Rules:
    """Rules from an editable team table (name, senior, min/max shifts) and a coverage table (shift x day)."""
    staff = [
        Employee(str(r["name"]).strip(), bool(r["senior"]), int(r["max shifts"]), int(r["min shifts"]))
        for _, r in team.iterrows()
        if str(r["name"]).strip()
    ]
    names = {e.name for e in staff}
    label_to_code = {v[0]: k for k, v in SHIFTS.items()}
    req = {(d, label_to_code[label]): int(need.loc[label, d]) for label in need.index for d in DAYS}
    prefs = {k: v for k, v in (SAMPLE_PREFS if prefer is None else prefer).items() if k[0] in names}
    return Rules(staff, req, prefer=prefs)


def validate_team(team: pd.DataFrame, need: pd.DataFrame) -> list[str]:
    problems = []
    names = [str(n).strip() for n in team["name"] if str(n).strip()]
    if len(names) < 2:
        problems.append("Add at least 2 people.")
    if len(set(n.lower() for n in names)) != len(names):
        problems.append("Names must be unique.")
    for _, r in team.iterrows():
        if not str(r["name"]).strip():
            continue
        lo, hi = r["min shifts"], r["max shifts"]
        if pd.isna(lo) or pd.isna(hi) or not (0 <= int(lo) <= int(hi) <= 7):
            problems.append(f"{r['name']}: shifts must satisfy 0 ≤ min ≤ max ≤ 7.")
    if need.isna().any().any() or (need < 0).any().any() or (need > 50).any().any():
        problems.append("Coverage must be whole numbers between 0 and 50.")
    return problems


def default_rules() -> Rules:
    return build_rules(sample_team(), sample_need())


def solve(rules: Rules, time_limit_s: float = 5.0, previous: Roster | None = None) -> Roster:
    m = cp_model.CpModel()
    names = [e.name for e in rules.staff]
    x = {(e, d, s): m.new_bool_var(f"x_{e}_{d}_{s}") for e in names for d in DAYS for s in SHIFTS}

    for e in names:
        for d in DAYS:
            m.add_at_most_one(x[e, d, s] for s in SHIFTS)
            if (e, d) in rules.off:
                for s in SHIFTS:
                    m.add(x[e, d, s] == 0)
        for d, s in ((d, s) for d in DAYS for s in SHIFTS):
            if (e, d, s) in rules.blocked:
                m.add(x[e, d, s] == 0)
        for i in range(len(DAYS) - 1):
            for a, b in FORBIDDEN_NEXT:
                m.add_bool_or([x[e, DAYS[i], a].Not(), x[e, DAYS[i + 1], b].Not()])
        k = rules.max_nights_in_row
        for i in range(len(DAYS) - k):
            m.add(sum(x[e, DAYS[i + j], "N"] for j in range(k + 1)) <= k)

    by_name = {e.name: e for e in rules.staff}
    for e in names:
        total = sum(x[e, d, s] for d in DAYS for s in SHIFTS)
        m.add(total <= by_name[e].max_shifts)
        available = sum(1 for d in DAYS if (e, d) not in rules.off)
        m.add(total >= min(by_name[e].min_shifts, by_name[e].max_shifts, max(available - 1, 0)))

    cost = []
    short, lack_senior = {}, {}
    seniors = [e.name for e in rules.staff if e.senior]
    for d in DAYS:
        for s in SHIFTS:
            need = rules.need.get((d, s), 0)
            short[d, s] = m.new_int_var(0, need, f"short_{d}_{s}")
            m.add(sum(x[e, d, s] for e in names) + short[d, s] >= need)
            m.add(sum(x[e, d, s] for e in names) <= need + 1)  # no big overstaffing
            cost.append(SHORTAGE_PENALTY * short[d, s])
            if need > 0:
                empty = m.new_bool_var(f"empty_{d}_{s}")
                m.add(sum(x[e, d, s] for e in names) + empty >= 1)
                cost.append(EMPTY_SHIFT_PENALTY * empty)
                lack_senior[d, s] = m.new_bool_var(f"nosenior_{d}_{s}")
                m.add(sum(x[e, d, s] for e in seniors) + lack_senior[d, s] >= 1)
                cost.append(NO_SENIOR_PENALTY * lack_senior[d, s])

    for (e, s), sign in rules.prefer.items():
        if e not in by_name or s not in SHIFTS:
            continue
        for d in DAYS:
            # likes: penalize each day NOT on that shift (when working); avoids: penalize each one worked
            if sign < 0:
                cost.append(PREF_PENALTY * x[e, d, s])
            else:
                other = sum(x[e, d, o] for o in SHIFTS if o != s)
                cost.append(PREF_PENALTY * other)

    # fairness: keep the heaviest night and weekend loads as low as possible
    max_nights = m.new_int_var(0, 7, "max_nights")
    max_weekend = m.new_int_var(0, 2 * len(WEEKEND), "max_weekend")
    for e in names:
        m.add(sum(x[e, d, "N"] for d in DAYS) <= max_nights)
        m.add(sum(x[e, d, s] for d in DAYS if d in WEEKEND for s in SHIFTS) <= max_weekend)
    cost += [FAIRNESS_WEIGHT * max_nights, FAIRNESS_WEIGHT * max_weekend]

    if previous is not None and previous.assign:
        for e in names:
            for d in DAYS:
                was = previous.assign.get((e, d))
                if was in SHIFTS:
                    cost.append(CHANGE_PENALTY * (1 - x[e, d, was]))
                    m.add_hint(x[e, d, was], 1)
                else:
                    cost.append(CHANGE_PENALTY * sum(x[e, d, s] for s in SHIFTS))

    m.minimize(sum(cost))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_workers = 8
    solver.parameters.random_seed = 7
    t0 = time.perf_counter()
    status = solver.solve(m)
    elapsed = time.perf_counter() - t0
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Roster(solver.status_name(status), {}, [], [], elapsed, 0.0)

    assign = {(e, d): s for e in names for d in DAYS for s in SHIFTS if solver.value(x[e, d, s])}
    shortages = [(d, s, solver.value(short[d, s])) for d in DAYS for s in SHIFTS if solver.value(short[d, s])]
    no_senior = [(d, s) for (d, s), v in lack_senior.items() if solver.value(v)]
    return Roster(solver.status_name(status), assign, shortages, no_senior, elapsed, solver.objective_value)


def _night_run(roster: Roster, name: str, i: int) -> int:
    run, j = 0, i - 1
    while j >= 0 and roster.assign.get((name, DAYS[j])) == "N":
        run, j = run + 1, j - 1
    j = i + 1
    while j < len(DAYS) and roster.assign.get((name, DAYS[j])) == "N":
        run, j = run + 1, j + 1
    return run


def unavailable_reason(rules: Rules, roster: Roster, e: Employee, day: str, shift: str) -> str | None:
    """Why this person isn't on this shift (None if they are)."""
    i, now = DAYS.index(day), roster.assign.get((e.name, day))
    if now == shift:
        return None
    if (e.name, day) in rules.off:
        return "on leave"
    if (e.name, day, shift) in rules.blocked:
        return "can't work this shift"
    if now:
        return "working another shift that day"
    if sum(1 for d in DAYS if (e.name, d) in roster.assign) >= e.max_shifts:
        return "at their weekly maximum"
    before = roster.assign.get((e.name, DAYS[i - 1])) if i > 0 else None
    after = roster.assign.get((e.name, DAYS[i + 1])) if i < len(DAYS) - 1 else None
    if (before, shift) in FORBIDDEN_NEXT or (shift, after) in FORBIDDEN_NEXT or (shift == "N" and _night_run(roster, e.name, i) >= rules.max_nights_in_row):
        return "blocked by rest time or night limits"
    return "needed elsewhere"


def explain_gap(rules: Rules, roster: Roster, day: str, shift: str) -> str:
    """Why nobody else could take this shift: count each person's blocking reason."""
    counts: dict[str, int] = {}
    for e in rules.staff:
        why = unavailable_reason(rules, roster, e, day, shift)
        if why:
            counts[why] = counts.get(why, 0) + 1
    return ", ".join(f"{n} {why}" for why, n in counts.items()) or "no one else on the team"


def senior_status(rules: Rules, roster: Roster, day: str, shift: str) -> str:
    return "; ".join(f"{e.name} {unavailable_reason(rules, roster, e, day, shift)}" for e in rules.staff if e.senior)


def diff(before: Roster, after: Roster, names: list[str]) -> list[str]:
    changes = []
    for e in names:
        for d in DAYS:
            a, b = before.assign.get((e, d)), after.assign.get((e, d))
            if a != b:
                fmt = lambda c: SHIFTS[c][0] if c else "off"  # noqa: E731
                changes.append(f"{e} {d}: {fmt(a)} → {fmt(b)}")
    return changes
