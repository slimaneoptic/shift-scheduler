"""Turn a manager's note ("Ahmed is off Thursday") into roster changes.

With an OpenRouter API key: an AI model reads the free-form note and returns changes that
match a strict JSON schema. Without a key, or when every free model is busy: a small
English rule-based parser.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import llm
from model import DAYS, SHIFTS, WEEKEND, Rules

KINDS = ("time_off", "cannot_work_shift", "staff_needed", "preference", "max_shifts")


@dataclass(frozen=True)
class Change:
    kind: str
    employee: str | None = None
    days: tuple[str, ...] = ()
    shift: str | None = None
    people: int | None = None
    likes: bool | None = None
    max_shifts: int | None = None

    def describe(self) -> str:
        when = _days_label(self.days)
        shift = SHIFTS[self.shift][0].lower() if self.shift else ""
        if self.kind == "time_off":
            return f"{self.employee} is off {when}"
        if self.kind == "cannot_work_shift":
            return f"{self.employee} can't work {shift}s {when}"
        if self.kind == "staff_needed":
            return f"{self.people} people needed on the {shift} shift {when}"
        if self.kind == "preference":
            return f"{self.employee} {'prefers' if self.likes else 'avoids'} {shift}s"
        return f"{self.employee} works at most {self.max_shifts} shifts"

    def apply(self, rules: Rules) -> None:
        if self.kind == "time_off":
            rules.off.update((self.employee, d) for d in self.days)
        elif self.kind == "cannot_work_shift":
            rules.blocked.update((self.employee, d, self.shift) for d in self.days)
        elif self.kind == "staff_needed":
            for d in self.days:
                rules.need[(d, self.shift)] = self.people
        elif self.kind == "preference":
            rules.prefer[(self.employee, self.shift)] = 1 if self.likes else -1
        elif self.kind == "max_shifts":
            for e in rules.staff:
                if e.name == self.employee:
                    e.max_shifts = self.max_shifts
                    e.min_shifts = min(e.min_shifts, self.max_shifts)


def _days_label(days: tuple[str, ...]) -> str:
    if not days:
        return ""
    if len(days) == 7:
        return "all week"
    if set(days) == WEEKEND and len(days) == len(WEEKEND):
        return "on the weekend"
    return "on " + ", ".join(days)


class ParseError(Exception):
    pass


@dataclass
class ParseResult:
    changes: list[Change]
    not_understood: list[str]
    engine: str  # "ai", "rules", or "rules (AI busy)"


# ---------------------------------------------------------------- validation (shared)


def _validate(raw: dict, names: list[str]) -> Change | None:
    kind = raw.get("kind")
    if kind not in KINDS:
        return None
    employee = raw.get("employee")
    days = tuple(d for d in DAYS if d in (raw.get("days") or [])) or tuple(DAYS)
    shift = raw.get("shift")
    if kind in ("time_off", "cannot_work_shift", "preference", "max_shifts") and employee not in names:
        return None
    if kind in ("cannot_work_shift", "staff_needed", "preference") and shift not in SHIFTS:
        return None
    if kind == "staff_needed" and not (isinstance(raw.get("people"), int) and 0 <= raw["people"] <= 20):
        return None
    if kind == "max_shifts" and not (isinstance(raw.get("max_shifts"), int) and 0 <= raw["max_shifts"] <= 7):
        return None
    if kind == "preference" and not isinstance(raw.get("likes"), bool):
        return None
    return Change(
        kind=kind,
        employee=employee if kind != "staff_needed" else None,
        days=days if kind in ("time_off", "cannot_work_shift", "staff_needed") else (),
        shift=shift if kind in ("cannot_work_shift", "staff_needed", "preference") else None,
        people=raw.get("people") if kind == "staff_needed" else None,
        likes=raw.get("likes") if kind == "preference" else None,
        max_shifts=raw.get("max_shifts") if kind == "max_shifts" else None,
    )


# ---------------------------------------------------------------- AI (OpenRouter)


def ai_available() -> bool:
    return llm.available()


def _schema(names: list[str]) -> dict:
    nullable = lambda s: {"anyOf": [s, {"type": "null"}]}  # noqa: E731
    change = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "employee": nullable({"type": "string", "enum": names}),
            "days": {"type": "array", "items": {"type": "string", "enum": DAYS}},
            "shift": nullable({"type": "string", "enum": list(SHIFTS)}),
            "people": nullable({"type": "integer"}),
            "likes": nullable({"type": "boolean"}),
            "max_shifts": nullable({"type": "integer"}),
        },
        "required": ["kind", "employee", "days", "shift", "people", "likes", "max_shifts"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"changes": {"type": "array", "items": change}, "not_understood": {"type": "array", "items": {"type": "string"}}},
        "required": ["changes", "not_understood"],
        "additionalProperties": False,
    }


SYSTEM = """You convert a shift manager's note into roster changes for a one-week schedule.

Staff: {names}
Days: Mon, Tue, Wed, Thu, Fri, Sat, Sun. The weekend is {weekend}.
Shifts: M = morning 07:00-15:00, E = evening 15:00-23:00, N = night 23:00-07:00.

Change kinds:
- time_off: an employee is off / on leave / unavailable on some days.
- cannot_work_shift: an employee can't work a given shift on some days.
- staff_needed: the number of people required on a shift on some days.
- preference: an employee likes (likes=true) or wants to avoid (likes=false) a shift type.
- max_shifts: the maximum number of shifts an employee works this week.

Days: "weekend" means {weekend}; "this week", "all week" or no day at all means all seven days.
Names: match the staff list even with typos, nicknames or Arabic script (for example أحمد is Ahmed). Never invent a person.
The note may be in English, Arabic or French and may contain several changes.
Put any part you cannot map to these kinds, or that mentions someone not on the staff list, in not_understood, quoted briefly.
Set fields that don't apply to a kind to null."""


def parse_with_ai(text: str, names: list[str], complete=llm.complete_json) -> ParseResult:
    system = SYSTEM.format(names=", ".join(names), weekend=" and ".join(sorted(WEEKEND, key=DAYS.index)))
    try:
        data, _model = complete([{"role": "system", "content": system}, {"role": "user", "content": text}], _schema(names), "roster_changes")
    except llm.LLMError as err:
        raise ParseError(str(err)) from err
    changes, rejected = [], [str(x) for x in data.get("not_understood", [])]
    for raw in data.get("changes", []):
        c = _validate(raw, names)
        (changes.append(c) if c else rejected.append(json.dumps(raw)))
    return ParseResult(changes, rejected, "ai")


# ---------------------------------------------------------------- rule-based fallback (English)

_DAY_WORDS = {
    "monday": "Mon", "mon": "Mon", "tuesday": "Tue", "tue": "Tue", "tues": "Tue", "wednesday": "Wed", "wed": "Wed",
    "thursday": "Thu", "thu": "Thu", "thurs": "Thu", "friday": "Fri", "fri": "Fri", "saturday": "Sat", "sat": "Sat",
    "sunday": "Sun", "sun": "Sun",
}
_SHIFT_WORDS = {"morning": "M", "mornings": "M", "evening": "E", "evenings": "E", "night": "N", "nights": "N", "day": "M", "days": "M"}


def _find_days(text: str) -> tuple[str, ...]:
    t = text.lower()
    if "weekend" in t:
        return tuple(d for d in DAYS if d in WEEKEND)
    found = {v for k, v in _DAY_WORDS.items() if re.search(rf"\b{k}\b", t)}
    return tuple(d for d in DAYS if d in found) or tuple(DAYS)


def _find_name(text: str, names: list[str]) -> str | None:
    low = text.lower()
    for n in names:
        if re.search(rf"\b{n.lower()}\b", low):
            return n
    return None


def _find_shift(text: str) -> str | None:
    t = text.lower()
    for word, code in _SHIFT_WORDS.items():
        if re.search(rf"\b{word}\b", t) and word not in ("day", "days"):
            return code
    return None


def parse_with_rules(text: str, names: list[str]) -> ParseResult:
    changes, rejected = [], []
    next_person = "|".join(re.escape(n) for n in names)
    splitter = rf"[.;\n]|,?\s\band\b(?=\s+(?:{next_person})\b)"  # split "X is off Mon and Y ..." but not "Mon and Tue"
    for part in [p.strip() for p in re.split(splitter, text, flags=re.I) if p and p.strip()]:
        low = part.lower()
        name = _find_name(part, names)
        shift = _find_shift(part)
        days = _find_days(part)
        raw = None
        num = re.search(r"\b(\d{1,2})\b", low)
        if re.search(r"\b(need|needs|require|requires|want)\b", low) and num and shift:
            raw = {"kind": "staff_needed", "days": list(days), "shift": shift, "people": int(num.group(1))}
        elif name and re.search(r"\b(at most|max|maximum|only|no more than)\b", low) and num and "shift" in low:
            raw = {"kind": "max_shifts", "employee": name, "max_shifts": int(num.group(1))}
        elif name and shift and re.search(r"\b(prefers?|likes?|loves?|wants?)\b", low) and not re.search(r"n't|not", low):
            raw = {"kind": "preference", "employee": name, "shift": shift, "likes": True}
        elif name and shift and re.search(r"\b(avoids?|hates?|dislikes?)\b|doesn't like|does not like|prefers not", low):
            raw = {"kind": "preference", "employee": name, "shift": shift, "likes": False}
        elif name and shift and re.search(r"can't|cannot|can not|won't|unable|no ", low):
            raw = {"kind": "cannot_work_shift", "employee": name, "days": list(days), "shift": shift}
        elif name and re.search(r"\b(off|leave|holiday|vacation|sick|away|absent|unavailable)\b", low):
            raw = {"kind": "time_off", "employee": name, "days": list(days)}
        change = _validate(raw, names) if raw else None
        (changes.append(change) if change else rejected.append(part))
    return ParseResult(changes, rejected, "rules")


def parse(text: str, names: list[str]) -> ParseResult:
    if ai_available():
        try:
            return parse_with_ai(text, names)
        except ParseError as err:
            if "rejected" in str(err):
                raise
            result = parse_with_rules(text, names)
            result.engine = "rules (AI busy)"
            return result
    return parse_with_rules(text, names)
