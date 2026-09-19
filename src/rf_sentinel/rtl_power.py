"""Контрольований RX-прийом і нормалізація CSV від rtl_power."""

from contextlib import ExitStack
import csv
import math
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Iterable

from rf_sentinel.acquisition import (DeviceIdentity, SpectrumSweep, SweepCoverage,
                                     SweepProfile, SweepProfileMetadata, SweepQuality)
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
    def __init__(self, device_index: int = 0, gain: float | None = None, stop=None):
        validate_device(device_index, gain)
        self._device_index = device_index
        self._gain = gain
        self._stop = stop
        self._process_lock = Lock()
        self._active_process = None
        self._shutdown_requested = False

    def acquire(self, profile: SweepProfile) -> SpectrumSweep:
        return _sweep_from_result(self._scan(profile))

    @staticmethod
    def _stop_process(process, *, deadline: float | None = None) -> None:
        """Terminate and reap rtl_power without an unbounded final wait."""
        def wait_timeout():
            return 2 if deadline is None else min(2, max(0.0, deadline - time.monotonic()))

        if process.poll() is not None:
            try:
                process.wait(timeout=wait_timeout())
            except subprocess.TimeoutExpired:
                raise ScanError(
                    "Не вдалося завершити rtl_power у відведений час",
                    reason="io_error",
                ) from None
            return
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=wait_timeout())
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=wait_timeout())
        except subprocess.TimeoutExpired:
            raise ScanError(
                "Не вдалося примусово завершити rtl_power у відведений час",
                reason="io_error",
            ) from None

    def _register_process(self, process) -> bool:
        with self._process_lock:
            self._active_process = process
            return not self._shutdown_requested

    def _start_process(self, command, **kwargs):
        """Atomically gate process creation against station shutdown."""
        with self._process_lock:
            if self._shutdown_requested:
                raise ScanError("Завершення прийому", reason="stopped")
            process = subprocess.Popen(command, **kwargs)
            self._active_process = process
            return process

    def _clear_process(self, process) -> None:
        with self._process_lock:
            if self._active_process is process:
                self._active_process = None

    def _stop_owned_process(self, process, *, deadline: float | None = None) -> None:
        self._stop_process(process, deadline=deadline)
        self._clear_process(process)

    @property
    def active_process(self):
        """Return the supervisor-visible rtl_power handle, if ownership is active."""
        with self._process_lock:
            return self._active_process

    def stop_active_process(self, *, deadline: float | None = None) -> bool:
        """Boundedly terminate, kill, and reap the currently owned rtl_power."""
        with self._process_lock:
            self._shutdown_requested = True
            process = self._active_process
        if process is None:
            return True
        self._stop_owned_process(process, deadline=deadline)
        return True

    def is_alive(self) -> bool:
        """Implement the station critical-owner protocol."""
        return self.active_process is not None

    def join(self, timeout: float | None = None) -> None:
        """Retry bounded child cleanup while the station lock remains owned."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        try:
            self.stop_active_process(deadline=deadline)
        except ScanError:
            return

    def _scan(self, profile: ScanProfile | SweepProfile,
              raw_path: Path | None = None) -> ScanResult:
        # flock захищає також від другого локального RF Sentinel process.
        import fcntl
        lock_path = Path(tempfile.gettempdir()) / f"rf-sentinel-rtl-{self._device_index}.lock"
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ScanError("SDR вже використовується", reason="device_busy") from None
            return self._run(profile, raw_path)

    def _run(self, profile: ScanProfile | SweepProfile,
             raw_path: Path | None = None) -> ScanResult:
        profile.__post_init__()
        command = [
            "rtl_power", "-f", f"{profile.low_hz}:{profile.high_hz}:{profile.bin_hz}",
            "-i", str(profile.integration_seconds), "-e", str(profile.duration_seconds),
            "-d", str(self._device_index),
        ]
        if isinstance(profile, SweepProfile):
            command[5:7] = ["-1"]
        if self._gain is not None:
            command += ["-g", str(self._gain)]
        command.append("-")
        # Дочірній процес не отримує Telegram credentials чи довільне environment.
        child_env = {"PATH": os.defpath, "TZ": "UTC", "LC_ALL": "C"}
        if "PATH" in os.environ:
            child_env["PATH"] = os.environ["PATH"]
        timeout_seconds = (90 if isinstance(profile, SweepProfile) else
                           profile.duration_seconds + max(15, profile.integration_seconds + 30))
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
                process = self._start_process(
                    command, shell=False, stdin=subprocess.DEVNULL, stdout=output,
                    stderr=diagnostics, env=child_env,
                )
                try:
                    while process.poll() is None:
                        if self._stop is not None and self._stop.is_set():
                            raise ScanError("Завершення прийому", reason="stopped")
                        if time.monotonic() - started > timeout_seconds:
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
                    self._stop_owned_process(process)
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


class RTLPowerSurveyAdapter:
    """Legacy multi-frame survey boundary for reporting and heatmap rendering."""

    def __init__(self, device_index: int = 0, gain: float | None = None):
        self._scanner = RTLPowerScanner(device_index, gain)

    def scan(self, profile: ScanProfile, raw_path: Path | None = None) -> ScanResult:
        return self._scanner._scan(profile, raw_path)


def _sweep_from_result(result: ScanResult) -> SpectrumSweep:
    """Normalize the adapter's internal single-frame result to the domain frame."""
    if len(result.spectrum.frames) != 1:
        raise ScanError("Очікувався один прохід спектра", reason="frame_count")
    edges = result.spectrum.edges_hz
    widths = [b - a for a, b in zip(edges, edges[1:])]
    if not widths or max(widths) - min(widths) > 2:
        raise ScanError("Неузгоджена ширина комірок", reason="bin_width")
    return SpectrumSweep(
        result.started_at, result.started_at + timedelta(seconds=result.duration_seconds),
        result.duration_seconds, edges[0], edges[-1],
        tuple((a + b) / 2 for a, b in zip(edges, edges[1:])),
        sum(widths) / len(widths), result.spectrum.frames[0].powers,
        result.backend, result.receiver, result.tuner,
        requested_profile=SweepProfileMetadata(
            result.profile.low_hz, result.profile.high_hz, result.profile.bin_hz,
            result.profile.integration_seconds, result.profile.duration_seconds),
        actual_profile=SweepProfileMetadata(
            edges[0], edges[-1], sum(widths) / len(widths),
            result.profile.integration_seconds, result.duration_seconds),
        device=DeviceIdentity(str(result.receiver), result.receiver, result.tuner),
        coverage=SweepCoverage("complete", len(result.spectrum.frames[0].powers),
                               len(result.spectrum.frames[0].powers), 1.0),
        quality=SweepQuality("valid"),
    )
