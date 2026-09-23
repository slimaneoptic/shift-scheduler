"""Staff rostering with OR-Tools CP-SAT.

Hard rules: one shift per day, rest time between shifts, contract limits, max nights in a row,
approved time off. Coverage and seniority are soft with a very high penalty, so the solver
always returns a roster and reports exactly where it falls short.
Soft goals: respect preferences, spread nights and weekends fairly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKEND = {"Fri", "Sat"}  # Gulf weekend by default; change for other regions
SHIFTS = {"M": ("Morning", "07:00–15:00"), "E": ("Evening", "15:00–23:00"), "N": ("Night", "23:00–07:00")}
# pairs (today, tomorrow) that leave less than 11 hours of rest
FORBIDDEN_NEXT = {("E", "M"), ("N", "M"), ("N", "E")}

SHORTAGE_PENALTY = 10_000
NO_SENIOR_PENALTY = 5_000
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


def default_rules() -> Rules:
    staff = [
        Employee("Ahmed", True, 5, 4), Employee("Sara", True, 5, 4), Employee("Omar", False, 5, 4),
        Employee("Fatima", False, 5, 4), Employee("Youssef", True, 5, 4), Employee("Layla", False, 5, 4),
        Employee("John", False, 5, 4), Employee("Maria", True, 5, 4), Employee("Khalid", True, 5, 4),
        Employee("Nadia", False, 3, 2), Employee("Priya", False, 5, 4), Employee("Hassan", False, 3, 2),
    ]
    need = {}
    for d in DAYS:
        weekend = d in WEEKEND
        need[(d, "M")] = 2 if weekend else 3
        need[(d, "E")] = 2 if weekend else 3
        need[(d, "N")] = 2
    prefer = {("Nadia", "M"): 1, ("Priya", "N"): -1, ("John", "N"): 1}
    return Rules(staff, need, prefer=prefer)


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


def diff(before: Roster, after: Roster, names: list[str]) -> list[str]:
    changes = []
    for e in names:
        for d in DAYS:
            a, b = before.assign.get((e, d)), after.assign.get((e, d))
            if a != b:
                fmt = lambda c: SHIFTS[c][0] if c else "off"  # noqa: E731
                changes.append(f"{e} {d}: {fmt(a)} → {fmt(b)}")
    return changes
