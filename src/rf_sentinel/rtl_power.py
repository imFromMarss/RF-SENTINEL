"""Controlled RX acquisition and normalization of rtl_power CSV."""

import csv
import math
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from typing import Iterable

from rf_sentinel.config import validate_device
from rf_sentinel.errors import ParseError, ScanError
from rf_sentinel.spectrum import ScanProfile, ScanResult, SpectrumData, SpectrumFrame

MAX_CSV_BYTES = 16 * 1024 * 1024
MAX_VALUES = 250_000


def parse_rtl_power(lines: Iterable[str]) -> SpectrumData:
    """Accept UTC CSV, including Osmocom's duplicated final FFT value.

    Reject incomplete/inconsistent sweeps instead of inventing missing coverage.
    Power is uncalibrated backend dB, not an absolute RF power measurement.
    """
    groups = {}
    value_count = 0
    byte_count = 0
    try:
        for line in lines:
            byte_count += len(line)
            if byte_count > MAX_CSV_BYTES:
                raise ValueError
            if not line.strip():
                continue
            columns = next(csv.reader([line], skipinitialspace=True))
            stamp = datetime.strptime(
                f"{columns[0].strip()} {columns[1].strip()}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=UTC)
            low, high, step = map(float, columns[2:5])
            samples = int(columns[5])
            powers = tuple(map(float, columns[6:]))
            if (not all(math.isfinite(v) for v in (low, high, step, *powers))
                    or not 0 < low < high or step <= 0 or samples <= 0 or not powers):
                raise ValueError
            bins = round((high - low) / step)
            # rtl_power prints step to .01 Hz and integer frequency endpoints.
            if bins < 1 or abs(bins * step - (high - low)) > 2 + bins * 0.0051:
                raise ValueError
            if len(powers) == bins + 1 and powers[-1] == powers[-2]:
                powers = powers[:-1]
            if len(powers) != bins:
                raise ValueError
            value_count += bins
            if value_count > MAX_VALUES:
                raise ValueError
            groups.setdefault(stamp, []).append((low, high, powers, samples))
        if not groups:
            raise ValueError
        frames = []
        shared_edges = None
        for stamp, chunks in sorted(groups.items()):
            edges, powers = [], []
            samples = 0
            for low, high, values, count in sorted(chunks):
                if edges and abs(edges[-1] - low) > 2:
                    raise ValueError
                if not edges:
                    edges.append(low)
                width = (high - low) / len(values)
                edges.extend(low + (i + 1) * width for i in range(len(values)))
                powers.extend(values)
                samples += count
            if shared_edges is None:
                shared_edges = tuple(edges)
            elif shared_edges != tuple(edges):
                raise ValueError
            frames.append(SpectrumFrame(stamp, tuple(powers), samples))
        return SpectrumData(shared_edges, tuple(frames))
    except (ValueError, IndexError, OverflowError, csv.Error):
        raise ParseError("Invalid or inconsistent rtl_power spectrum data") from None


class RTLPowerScanner:
    def __init__(self, device_index: int = 0, gain: float | None = None):
        validate_device(device_index, gain)
        self._device_index = device_index
        self._gain = gain

    def scan(self, profile: ScanProfile) -> ScanResult:
        profile.__post_init__()
        command = [
            "rtl_power", "-f", f"{profile.low_hz}:{profile.high_hz}:{profile.bin_hz}",
            "-i", str(profile.integration_seconds), "-e", str(profile.duration_seconds),
            "-d", str(self._device_index),
        ]
        if self._gain is not None:
            command += ["-g", str(self._gain)]
        command.append("-")
        # Do not pass Telegram credentials or arbitrary environment to the child.
        child_env = {"PATH": os.defpath, "TZ": "UTC", "LC_ALL": "C"}
        if "PATH" in os.environ:
            child_env["PATH"] = os.environ["PATH"]
        started_at = datetime.now(UTC)
        started = time.monotonic()
        try:
            with tempfile.TemporaryFile() as output:
                # File-backed output avoids unbounded communicate() RAM allocation.
                with subprocess.Popen(
                    command, shell=False, stdin=subprocess.DEVNULL, stdout=output,
                    stderr=subprocess.DEVNULL, env=child_env,
                ) as process:
                    try:
                        while process.poll() is None:
                            if time.monotonic() - started > profile.duration_seconds + 15:
                                raise ScanError("rtl_power scan timed out")
                            if os.fstat(output.fileno()).st_size > MAX_CSV_BYTES:
                                raise ScanError("rtl_power output exceeded size limit")
                            time.sleep(0.05)
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.wait()
                    if process.returncode != 0:
                        raise ScanError("rtl_power failed; check device availability")
                output.seek(0)
                raw = output.read(MAX_CSV_BYTES + 1)
                if len(raw) > MAX_CSV_BYTES:
                    raise ScanError("rtl_power output exceeded size limit")
            spectrum = parse_rtl_power(raw.decode("ascii").splitlines())
        except FileNotFoundError:
            raise ScanError("rtl_power executable is not installed") from None
        except (OSError, UnicodeError):
            raise ScanError("rtl_power acquisition I/O failed") from None
        if (spectrum.edges_hz[0] > profile.low_hz + 2
                or spectrum.edges_hz[-1] < profile.high_hz - 2
                or spectrum.edges_hz[0] < profile.low_hz - profile.bin_hz
                or spectrum.edges_hz[-1] > profile.high_hz + profile.bin_hz):
            raise ScanError("rtl_power output does not cover the requested survey range")
        return ScanResult("rtl_power", profile, started_at,
                          time.monotonic() - started, spectrum)
