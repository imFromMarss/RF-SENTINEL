"""Small, shared checkpoint primitives for characterization runners."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def _exactly_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return (left.keys() == right.keys()
                and all(_exactly_equal(left[key], right[key]) for key in left))
    if isinstance(left, list):
        return (len(left) == len(right)
                and all(_exactly_equal(a, b) for a, b in zip(left, right, strict=True)))
    return left == right


def canonical_point_identity(fields: Mapping[str, int | str]) -> str:
    """Return the established canonical JSON identity for one planned point."""
    for key, value in fields.items():
        if not isinstance(key, str):
            raise TypeError("checkpoint identity keys must be strings")
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError("checkpoint identity values must be int or str")
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))


def validate_checkpoint_envelope(
        payload: object, *, expected_schema_version: str,
        expected_configuration: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Validate schema/configuration and return the required stored points."""
    if not isinstance(payload, dict):
        raise ValueError("Checkpoint root must be an object")
    if payload.get("schema_version") != expected_schema_version:
        raise ValueError("Checkpoint schema_version does not match expected schema")
    if not _exactly_equal(payload.get("configuration"), expected_configuration):
        raise ValueError("Resume configuration does not match existing dataset")
    if "points" not in payload:
        raise ValueError("Checkpoint is missing required points field")
    points = payload["points"]
    if (not isinstance(points, list)
            or any(not isinstance(point, dict) for point in points)):
        raise ValueError("Checkpoint points must be a list of objects")
    return points


def validate_checkpoint_identities(
        *, planned_identities: Sequence[str], stored_identities: Sequence[str],
        recomputed_identities: Sequence[str]) -> set[str]:
    """Validate checkpoint membership and caller-recomputed record identities."""
    planned = list(planned_identities)
    if any(not isinstance(identity, str) for identity in planned):
        raise ValueError("Planned checkpoint identities must be strings")
    if len(planned) != len(set(planned)):
        raise ValueError("Planned checkpoint identities must be unique")

    stored = list(stored_identities)
    if len(stored) != len(recomputed_identities):
        raise ValueError("Stored and recomputed checkpoint identities are misaligned")
    if any(not isinstance(identity, str) for identity in stored):
        raise ValueError("Stored checkpoint identities must be strings")
    if len(stored) != len(set(stored)):
        raise ValueError("Stored checkpoint identities must be unique")

    planned_set = set(planned)
    stored_set = set(stored)
    if not stored_set <= planned_set:
        raise ValueError("Stored checkpoint identity is not present in the plan")
    if any(stored_identity != recomputed_identity
           for stored_identity, recomputed_identity
           in zip(stored, recomputed_identities, strict=True)):
        raise ValueError("Stored checkpoint identity does not match record fields")
    return stored_set


def atomic_write_json(path: Path, payload: object) -> None:
    """Write the established pretty JSON checkpoint through a sibling temp file."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
