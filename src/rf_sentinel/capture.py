"""Shared bounded rtl_power capture plumbing."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Callable, Sequence


_GAIN_RE = re.compile(r"Tuner gain set to\s+([-+0-9.]+)\s+dB", re.IGNORECASE)
_FFT_BIN_RE = re.compile(r"FFT bin size:\s*([-+0-9.]+)\s*Hz", re.IGNORECASE)


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
