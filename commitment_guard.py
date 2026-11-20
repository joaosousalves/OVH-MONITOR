"""Fail-closed checks for KS-LE-6 month-to-month, no fixed commitment.

P1M is a billing/order duration, NOT proof that a contract has no minimum
engagement. Reject explicit conflicting metadata, and never interpret
missing contract metadata as provider-verified no-commitment terms.
"""
from __future__ import annotations

import re
from typing import Any


class CommitmentSafetyError(Exception):
    """Explicit long-term commitment or unrecognized contract indicator."""


_DURATION_KEYS = frozenset((
    "commitmentDuration", "engagementDuration", "minimumCommitment",
    "minimumCommitmentDuration", "commitmentMonths", "engagementMonths",
))
_BOOLEAN_KEYS = frozenset(("commitment", "engagement"))
_ZERO_TEXT = frozenset(("none", "no_commitment", "without_commitment", "sans_engagement", "0", "p0m"))


def _read_indicator(key: str, value: Any) -> bool | None:
    """Return False for explicit zero, True for obligation, None for absent."""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        if "duration" in value:
            return _read_indicator("commitmentDuration", value["duration"])
        if "months" in value:
            return _read_indicator("commitmentMonths", value["months"])
        raise CommitmentSafetyError("Unrecognized commitment object: no purchase allowed")
    if isinstance(value, bool):
        if key in _BOOLEAN_KEYS:
            return value
        raise CommitmentSafetyError("Invalid commitment duration boolean")
    if type(value) is int and 0 <= value <= 240:
        return value > 0
    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in _ZERO_TEXT:
            return False
        match = re.fullmatch(r"p([0-9]{1,3})m", normalized)
        if match:
            return int(match.group(1)) > 0
        match = re.fullmatch(r"([0-9]{1,3})m?", normalized)
        if match:
            return int(match.group(1)) > 0
    raise CommitmentSafetyError("Unknown commitment indicator: no purchase allowed")


def inspect_commitment(*records: Any) -> bool:
    """Reject any explicitly nonzero commitment in bounded structured data.

    True merely means at least one explicit zero-commitment indicator was
    observed; it does NOT replace reading all seller contract conditions.
    """
    zero_found = False
    for record in records:
        if not isinstance(record, dict):
            continue
        # Only documented machine-readable top-level fields and nested
        # contract objects; never guess based on legal free text.
        candidates = [record]
        if isinstance(record.get("contracts"), list):
            candidates += [c for c in record["contracts"] if isinstance(c, dict)]
        for obj in candidates:
            for key in _DURATION_KEYS | _BOOLEAN_KEYS:
                if key not in obj:
                    continue
                indicator = _read_indicator(key, obj[key])
                if indicator is True:
                    raise CommitmentSafetyError(
                        "Fixed-term commitment detected; only month-to-month without commitment is permitted"
                    )
                if indicator is False:
                    zero_found = True
    return zero_found
