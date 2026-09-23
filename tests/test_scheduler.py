import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model import DAYS, FORBIDDEN_NEXT, default_rules, diff, solve  # noqa: E402
from nl_parser import ParseError, parse_with_ai, parse_with_rules  # noqa: E402

NAMES = [e.name for e in default_rules().staff]


def _check_hard_rules(rules, roster):
    for e in rules.staff:
        worked = [d for d in DAYS if (e.name, d) in roster.assign]
        assert len(worked) <= e.max_shifts
        for (name, d) in rules.off:
            assert (name, d) not in roster.assign
        for i in range(len(DAYS) - 1):
            a, b = roster.assign.get((e.name, DAYS[i])), roster.assign.get((e.name, DAYS[i + 1]))
            assert (a, b) not in FORBIDDEN_NEXT, f"{e.name} gets too little rest"
    for (name, d, s) in rules.blocked:
        assert roster.assign.get((name, d)) != s


def test_default_week_is_fully_covered_with_a_senior_everywhere():
    rules = default_rules()
    roster = solve(rules, 5)
    assert roster.status in ("OPTIMAL", "FEASIBLE")
    assert roster.shortages == [] and roster.no_senior == []
    _check_hard_rules(rules, roster)


def test_a_change_moves_few_shifts():
    rules = default_rules()
    before = solve(rules, 5)
    who, day = next(iter(before.assign))
    rules.off.add((who, day))
    after = solve(rules, 5, previous=before)
    changes = diff(before, after, NAMES)
    assert changes and len(changes) <= 6
    _check_hard_rules(rules, after)


def test_impossible_week_reports_shortages_instead_of_failing():
    rules = default_rules()
    for e in ("Ahmed", "Sara", "Maria", "Omar"):
        rules.off.update((e, d) for d in DAYS)
    roster = solve(rules, 5)
    assert roster.status in ("OPTIMAL", "FEASIBLE")
    assert roster.shortages


def test_rule_parser_understands_common_notes():
    r = parse_with_rules("Ahmed is off Thursday. Sara can't do nights this week; We need 4 people on Friday evening", NAMES)
    kinds = [(c.kind, c.employee, c.days, c.shift, c.people) for c in r.changes]
    assert ("time_off", "Ahmed", ("Thu",), None, None) in kinds
    assert ("cannot_work_shift", "Sara", tuple(DAYS), "N", None) in kinds
    assert ("staff_needed", None, ("Fri",), "E", 4) in kinds
    r2 = parse_with_rules("Nadia prefers mornings and Priya prefers not to work nights", NAMES)
    assert {(c.employee, c.likes) for c in r2.changes} == {("Nadia", True), ("Priya", False)}
    assert parse_with_rules("Maria can work at most 3 shifts", NAMES).changes[0].max_shifts == 3
    assert parse_with_rules("Bob is off Monday", NAMES).not_understood == ["Bob is off Monday"]


def test_ai_path_validates_output_and_sends_schema():
    payload = {
        "changes": [
            {"kind": "time_off", "employee": "Fatima", "days": ["Sun"], "shift": None, "people": None, "likes": None, "max_shifts": None},
            {"kind": "staff_needed", "employee": None, "days": ["Fri"], "shift": "E", "people": 99, "likes": None, "max_shifts": None},
        ],
        "not_understood": ["the coffee machine is broken"],
    }
    seen = {}

    def fake_complete(messages, schema, name):
        seen.update(messages=messages, schema=schema)
        return payload, "fake-model"

    r = parse_with_ai("Fatima is off Sunday, need 99 on Friday evening, coffee machine broken", NAMES, complete=fake_complete)
    assert [c.describe() for c in r.changes] == ["Fatima is off on Sun"]
    assert len(r.not_understood) == 2  # 99 people is rejected by validation
    assert seen["schema"]["additionalProperties"] is False
    assert "Ahmed" in seen["messages"][0]["content"]


def test_busy_ai_falls_back_to_rule_reader(monkeypatch):
    import nl_parser

    def busy(*a, **k):
        raise ParseError("All AI models are busy right now.")

    monkeypatch.setattr(nl_parser.llm, "available", lambda: True)
    monkeypatch.setattr(nl_parser, "parse_with_ai", busy)
    r = nl_parser.parse("Ahmed is off Thursday", NAMES)
    assert r.engine == "rules (AI busy)" and r.changes[0].days == ("Thu",)


def test_and_between_days_is_not_split_but_and_between_people_is():
    r = parse_with_rules("Omar is sick on Tuesday and Wednesday", NAMES)
    assert [c.days for c in r.changes] == [("Tue", "Wed")] and not r.not_understood
    r2 = parse_with_rules("Omar is off Monday and Layla is off Sunday", NAMES)
    assert {(c.employee, c.days) for c in r2.changes} == {("Omar", ("Mon",)), ("Layla", ("Sun",))}
