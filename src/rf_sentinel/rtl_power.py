"""Контрольований RX-прийом і нормалізація CSV від rtl_power."""

from contextlib import ExitStack
import csv
import math
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from rf_sentinel.config import validate_device
from rf_sentinel.errors import ParseError, ScanError
from rf_sentinel.spectrum import ScanProfile, ScanResult, SpectrumData, SpectrumFrame

MAX_CSV_BYTES = 64 * 1024 * 1024
MAX_VALUES = 1_000_000


def parse_rtl_power(lines: Iterable[str]) -> SpectrumData:
    """Читає UTC CSV, враховуючи дубль останнього FFT-значення в Osmocom.

    Неповні проходи відхиляються; відсутнє покриття не підміняється вимірюваннями.
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
            # rtl_power округлює крок до 0,01 Гц, а межі — до цілих Гц.
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
        raise ParseError(
            "Некоректні або неузгоджені дані спектра rtl_power",
            reason="parser_malformed",
        ) from None


class RTLPowerScanner:
    def __init__(self, device_index: int = 0, gain: float | None = None):
        validate_device(device_index, gain)
        self._device_index = device_index
        self._gain = gain

    def scan(self, profile: ScanProfile, raw_path: Path | None = None) -> ScanResult:
        profile.__post_init__()
        command = [
            "rtl_power", "-f", f"{profile.low_hz}:{profile.high_hz}:{profile.bin_hz}",
            "-i", str(profile.integration_seconds), "-e", str(profile.duration_seconds),
            "-d", str(self._device_index),
        ]
        if self._gain is not None:
            command += ["-g", str(self._gain)]
        command.append("-")
        # Дочірній процес не отримує Telegram credentials чи довільне environment.
        child_env = {"PATH": os.defpath, "TZ": "UTC", "LC_ALL": "C"}
        if "PATH" in os.environ:
            child_env["PATH"] = os.environ["PATH"]
        started_at = datetime.now(UTC)
        started = time.monotonic()
        try:
            with ExitStack() as stack:
                output = stack.enter_context(raw_path.open("xb+") if raw_path is not None
                                             else tempfile.TemporaryFile())
                diagnostics = stack.enter_context(
                    raw_path.with_name("rtl_power.stderr.txt").open("xb+")
                    if raw_path is not None else tempfile.TemporaryFile()
                )
                # Файлові буфери обмежують використання RAM; raw CSV зберігається і при відмові.
                with subprocess.Popen(
                    command, shell=False, stdin=subprocess.DEVNULL, stdout=output,
                    stderr=diagnostics, env=child_env,
                ) as process:
                    try:
                        while process.poll() is None:
                            if time.monotonic() - started > profile.duration_seconds + max(15, profile.integration_seconds + 30):
                                raise ScanError(
                                    "Перевищено час очікування сканування rtl_power",
                                    reason="timeout",
                                )
                            if os.fstat(output.fileno()).st_size > MAX_CSV_BYTES:
                                raise ScanError(
                                    "Дані rtl_power перевищили ліміт розміру",
                                    reason="output_too_large",
                                )
                            if os.fstat(diagnostics.fileno()).st_size > 1024 * 1024:
                                raise ScanError(
                                    "Діагностика rtl_power перевищила ліміт розміру",
                                    reason="stderr_too_large",
                                )
                            time.sleep(0.05)
                    finally:
                        if process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()
                        else:
                            process.wait()
                    if process.returncode != 0:
                        raise ScanError(
                            "Помилка rtl_power; перевірте доступність пристрою",
                            reason="subprocess_exit",
                            returncode=process.returncode,
                        )
                diagnostics.seek(0)
                diagnostic = diagnostics.read(1024 * 1024 + 1)
                tuner = "R820T" if b"R820T" in diagnostic else "невідомо"
                if b"R828D" in diagnostic:
                    tuner = "R828D"
                # Один PLL warning можливий при початковому калібруванні до налаштування частоти.
                if diagnostic.count(b"PLL not locked") > 1 or b"No valid PLL" in diagnostic:
                    raise ScanError(
                        "Тюнер не підтвердив стабільне налаштування частоти",
                        reason="tuner_pll",
                    )
                output.seek(0)
                raw = output.read(MAX_CSV_BYTES + 1)
                if len(raw) > MAX_CSV_BYTES:
                    raise ScanError(
                        "Дані rtl_power перевищили ліміт розміру",
                        reason="output_too_large",
                    )
            spectrum = parse_rtl_power(raw.decode("ascii").splitlines())
        except FileNotFoundError:
            raise ScanError(
                "Програму rtl_power не встановлено", reason="executable_missing"
            ) from None
        except (OSError, UnicodeError):
            raise ScanError(
                "Помилка введення/виведення під час прийому rtl_power",
                reason="io_error",
            ) from None
        if (spectrum.edges_hz[0] > profile.low_hz + profile.bin_hz
                or spectrum.edges_hz[-1] < profile.high_hz - profile.bin_hz
                or spectrum.edges_hz[0] < profile.low_hz - profile.bin_hz
                or spectrum.edges_hz[-1] > profile.high_hz + profile.bin_hz):
            raise ScanError(
                "Дані rtl_power не покривають запитаний діапазон огляду",
                reason="incomplete_coverage",
            )
        return ScanResult("rtl_power", profile, started_at,
                          time.monotonic() - started, spectrum, "RTL-SDR", tuner, self._gain)
