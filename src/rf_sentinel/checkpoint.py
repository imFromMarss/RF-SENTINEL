"""Small, shared checkpoint primitives for characterization runners."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


def canonical_point_identity(fields: Mapping[str, int | str]) -> str:
    """Return the established canonical JSON identity for one planned point."""
    for key, value in fields.items():
        if not isinstance(key, str):
            raise TypeError("checkpoint identity keys must be strings")
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError("checkpoint identity values must be int or str")
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))


def atomic_write_json(path: Path, payload: object) -> None:
    """Write the established pretty JSON checkpoint through a sibling temp file."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
