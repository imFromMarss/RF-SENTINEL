"""Shared bounded rtl_power capture plumbing."""

from __future__ import annotations

import os
import math
from pathlib import Path
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence


_GAIN_RE = re.compile(r"Tuner gain set to\s+([-+0-9.]+)\s+dB", re.IGNORECASE)
_FFT_BIN_RE = re.compile(r"FFT bin size:\s*([-+0-9.]+)\s*Hz", re.IGNORECASE)


@dataclass(frozen=True)
class RTLPowerRow:
    low: float
    high: float
    step: float
    power_bins: tuple[float, ...]


def decode_rtl_power_row(
        columns: Sequence[str], *, require_positive_low: bool) -> RTLPowerRow | None:
    """Decode one rtl_power CSV row, or return None for an invalid row."""
    if len(columns) < 7:
        return None
    try:
        low, high, step = map(float, columns[2:5])
        values = tuple(float(value) for value in columns[6:])
    except (ValueError, TypeError):
        return None
    if (not all(math.isfinite(value) for value in (low, high, step, *values))
            or not values):
        return None
    invalid_range = (not 0 < low < high) if require_positive_low else low >= high
    if invalid_range or step <= 0:
        return None
    expected_bins = round((high - low) / step)
    if (expected_bins <= 0
            or abs(expected_bins * step - (high - low))
            > 2 + expected_bins * 0.0051):
        return None
    if (len(values) == expected_bins + 1
            and values[-1] == values[-2]):
        values = values[:-1]
    if len(values) != expected_bins:
        return None
    return RTLPowerRow(low, high, step, values)


def invoke_rtl_power(command: Sequence[str], *, stderr_path: Path,
                     timeout: float, runner: Callable = subprocess.run) -> int:
    """Run rtl_power with bounded, artifact-backed stdio and return its raw rc."""
    with stderr_path.open("wb") as diagnostics:
        completed = runner(
            command, shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=diagnostics,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "TZ": "UTC", "LC_ALL": "C"},
            timeout=timeout, check=False,
        )
    return completed.returncode


def raw_csv_within_limit(path: Path, max_bytes: int) -> bool:
    """Return whether a raw CSV exists and is no larger than the given limit."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    return size <= max_bytes


def read_stderr_text(path: Path) -> str:
    """Read an rtl_power stderr artifact as replacement-decoded UTF-8."""
    return path.read_text(encoding="utf-8", errors="replace")


def _diagnostics(stderr_text: str) -> tuple[float | None, float | None, list[str]]:
    """Parse existing rtl_power diagnostics without changing warning semantics."""
    gain_match = _GAIN_RE.search(stderr_text)
    bin_match = _FFT_BIN_RE.search(stderr_text)
    warnings = [
        line.strip() for line in stderr_text.splitlines()
        if ("pll not locked" in line.lower() or "warning" in line.lower()
            or "error" in line.lower() or "no e4000" in line.lower())
    ]
    return (
        float(gain_match.group(1)) if gain_match else None,
        float(bin_match.group(1)) if bin_match else None,
        warnings,
    )
