"""Small, deliberately non-executable parser for ADL decision specifications."""

from __future__ import annotations

import re
import shlex

from .types import ChoiceQuestion, DecisionSpec, NoulQuestion, ScoreQuestion


class DecisionLanguageError(ValueError):
    """Raised when an ADL source file violates the supported grammar."""


class DecisionLanguageParser:
    _DECISION = re.compile(r"^DECISION\s+([A-Za-z_][A-Za-z0-9_]*)\s*:$")
    _CHOICE = re.compile(r"^ASK\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*CHOICE\s*\[(.*)\]$")
    _SCORE = re.compile(
        r"^ASK\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*SCORE\s+(-?\d+)\.\.(-?\d+)$"
    )
    _NOUL = re.compile(r"^ASK\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*NOUL\s+(.+)$")

    def parse(self, source: str) -> DecisionSpec:
        if not isinstance(source, str) or not source.strip():
            raise DecisionLanguageError("ADL source cannot be empty")
        name = None
        section = None
        state_fields: list[str] = []
        questions = []
        outputs: list[str] = []

        for line_number, raw_line in enumerate(source.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = self._DECISION.match(line)
            if match:
                if name is not None:
                    raise DecisionLanguageError("only one DECISION block is supported")
                name = match.group(1)
                continue
            if line in {"STATE:", "OUTPUT:"}:
                if name is None:
                    raise DecisionLanguageError(
                        f"line {line_number}: DECISION must be declared first"
                    )
                section = line[:-1].lower()
                continue
            if section in {"state", "output"} and line.startswith("- "):
                value = line[2:].strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", value):
                    raise DecisionLanguageError(
                        f"line {line_number}: invalid {section} identifier"
                    )
                (state_fields if section == "state" else outputs).append(value)
                continue
            match = self._CHOICE.match(line)
            if match:
                options = tuple(
                    item.strip().strip("\"'")
                    for item in match.group(2).split(",")
                    if item.strip()
                )
                questions.append(ChoiceQuestion(match.group(1), options))
                section = None
                continue
            match = self._SCORE.match(line)
            if match:
                questions.append(
                    ScoreQuestion(
                        match.group(1), int(match.group(2)), int(match.group(3))
                    )
                )
                section = None
                continue
            match = self._NOUL.match(line)
            if match:
                try:
                    statement = shlex.split(match.group(2))
                except ValueError as exc:
                    raise DecisionLanguageError(
                        f"line {line_number}: invalid NOUL statement"
                    ) from exc
                if len(statement) != 1:
                    raise DecisionLanguageError(
                        f"line {line_number}: NOUL requires one quoted statement"
                    )
                questions.append(NoulQuestion(match.group(1), statement[0]))
                section = None
                continue
            raise DecisionLanguageError(
                f"line {line_number}: unsupported or misplaced statement"
            )

        if name is None:
            raise DecisionLanguageError("missing DECISION declaration")
        try:
            return DecisionSpec(
                name=name,
                state_fields=tuple(state_fields),
                questions=tuple(questions),
                outputs=tuple(outputs) or ("decision", "probabilities", "reasons"),
            )
        except ValueError as exc:
            raise DecisionLanguageError(str(exc)) from exc


def parse_decision_spec(source: str) -> DecisionSpec:
    """Parse a safe ADL specification; no expressions or Python code are evaluated."""
    return DecisionLanguageParser().parse(source)
