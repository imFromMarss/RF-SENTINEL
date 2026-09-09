"""Durable SQLite storage for canonical spectrum sweeps."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import struct
from typing import Iterable

from rf_sentinel.acquisition import (DeviceIdentity, ErrorClassification,
                                     MeasurementReceipt, MeasurementSink,
                                     SpectrumSweep, SweepCoverage, SweepQuality,
                                     SweepProfileMetadata)
from rf_sentinel.errors import MeasurementPersistenceError


SCHEMA_VERSION = 1
SCHEMA_NAME = "spectrum-sweeps.v1"
_FLOAT64 = struct.Struct("<d")


def _pack(values: Iterable[float]) -> bytes:
    values = tuple(values)
    return struct.pack(f"<{len(values)}d", *values) if values else b""


def _unpack(blob: bytes | None, count: int, field: str) -> tuple[float, ...]:
    if blob is None:
        if count == 0:
            return ()
        raise ValueError(f"missing {field} payload")
    if len(blob) != count * _FLOAT64.size:
        raise ValueError(f"invalid {field} payload length")
    return struct.unpack(f"<{count}d", blob) if count else ()


def _timestamp(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return int(value.timestamp() * 1_000_000)


def _profile(value: SweepProfileMetadata | None) -> dict | None:
    return None if value is None else {
        "low_hz": value.low_hz, "high_hz": value.high_hz, "bin_hz": value.bin_hz,
        "integration_seconds": value.integration_seconds,
        "duration_seconds": value.duration_seconds,
    }


def _metadata(sweep: SpectrumSweep) -> str:
    data = {
        "schema_version": sweep.schema_version,
        "sweep_id": sweep.sweep_id,
        "sequence": sweep.sequence,
        "started_at": sweep.started_at.isoformat(),
        "finished_at": sweep.finished_at.isoformat(),
        "duration_seconds": sweep.duration_seconds,
        "start_hz": sweep.start_hz,
        "stop_hz": sweep.stop_hz,
        "bin_width_hz": sweep.bin_width_hz,
        "backend": sweep.backend,
        "receiver": sweep.receiver,
        "tuner": sweep.tuner,
        "status": sweep.status,
        "correlation_id": sweep.correlation_id,
        "requested_profile": _profile(sweep.requested_profile),
        "actual_profile": _profile(sweep.actual_profile),
        "device": {"device_id": sweep.device.device_id, "model": sweep.device.model,
                    "tuner": sweep.device.tuner},
        "tool_version": sweep.tool_version,
        "coverage": {"status": sweep.coverage.status, "expected_bins": sweep.coverage.expected_bins,
                      "observed_bins": sweep.coverage.observed_bins,
                      "fraction": sweep.coverage.fraction},
        "quality": {"status": sweep.quality.status, "flags": list(sweep.quality.flags)},
        "error_classification": None if sweep.error_classification is None else {
            "category": sweep.error_classification.category,
            "code": sweep.error_classification.code,
        },
        "payload_encoding": "float64-le-v1",
    }
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class SQLiteMeasurementSink(MeasurementSink):
    """One SQLite row per sweep, with atomic insert and durable reopen support."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = sqlite3.connect(self.path)
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._initialize()
        except (OSError, sqlite3.Error) as error:
            raise MeasurementPersistenceError("Could not open SQLite measurement store") from error

    def _initialize(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise MeasurementPersistenceError(f"Unsupported SQLite schema version: {version}")
        if version == 0:
            with self._db:
                self._db.executescript("""
                    CREATE TABLE IF NOT EXISTS sweeps (
                        sweep_id TEXT PRIMARY KEY,
                        started_at_us INTEGER NOT NULL,
                        finished_at_us INTEGER NOT NULL,
                        outcome TEXT NOT NULL CHECK (outcome IN ('success', 'partial', 'failed')),
                        metadata_json TEXT NOT NULL,
                        frequency_count INTEGER NOT NULL,
                        frequencies_blob BLOB,
                        power_count INTEGER NOT NULL,
                        powers_blob BLOB
                    );
                    CREATE INDEX IF NOT EXISTS idx_sweeps_started_at
                        ON sweeps(started_at_us);
                    CREATE INDEX IF NOT EXISTS idx_sweeps_sweep_id
                        ON sweeps(sweep_id);
                    CREATE INDEX IF NOT EXISTS idx_sweeps_outcome
                        ON sweeps(outcome);
                    PRAGMA user_version = 1;
                """)

    def store_sweep(self, sweep: SpectrumSweep) -> MeasurementReceipt:
        if not isinstance(sweep, SpectrumSweep):
            raise MeasurementPersistenceError("Only SpectrumSweep records can be persisted")
        try:
            metadata = _metadata(sweep)
            started = _timestamp(sweep.started_at)
            finished = _timestamp(sweep.finished_at)
            frequencies = _pack(sweep.frequencies_hz)
            powers = _pack(sweep.powers)
            with self._db:
                self._db.execute(
                    """INSERT INTO sweeps
                    (sweep_id, started_at_us, finished_at_us, outcome, metadata_json,
                     frequency_count, frequencies_blob, power_count, powers_blob)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (sweep.sweep_id, started, finished, sweep.outcome, metadata,
                     len(sweep.frequencies_hz), frequencies if sweep.frequencies_hz else None,
                     len(sweep.powers), powers if sweep.powers else None),
                )
        except (OSError, sqlite3.Error, TypeError, ValueError, struct.error) as error:
            raise MeasurementPersistenceError("Could not persist spectrum sweep") from error
        return MeasurementReceipt(sweep.sweep_id, "persisted")

    def fetch_sweep(self, sweep_id: str) -> SpectrumSweep | None:
        try:
            row = self._db.execute(
                "SELECT metadata_json, frequency_count, frequencies_blob, power_count, powers_blob "
                "FROM sweeps WHERE sweep_id = ?", (sweep_id,)).fetchone()
            return None if row is None else self._decode(row)
        except (sqlite3.Error, TypeError, ValueError, KeyError, struct.error, json.JSONDecodeError) as error:
            raise MeasurementPersistenceError("Could not read spectrum sweep") from error

    def query_sweeps(self, start: datetime, end: datetime) -> list[SpectrumSweep]:
        try:
            start_us, end_us = _timestamp(start), _timestamp(end)
            rows = self._db.execute(
                "SELECT metadata_json, frequency_count, frequencies_blob, power_count, powers_blob "
                "FROM sweeps WHERE started_at_us >= ? AND started_at_us < ? "
                "ORDER BY started_at_us, sweep_id", (start_us, end_us)).fetchall()
            return [self._decode(row) for row in rows]
        except (sqlite3.Error, TypeError, ValueError, KeyError, struct.error, json.JSONDecodeError) as error:
            raise MeasurementPersistenceError("Could not query spectrum sweeps") from error

    def _decode(self, row) -> SpectrumSweep:
        data = json.loads(row[0])
        frequencies = _unpack(row[2], row[1], "frequency")
        powers = _unpack(row[4], row[3], "power")

        def profile(value):
            return None if value is None else SweepProfileMetadata(**value)

        coverage = SweepCoverage(**data["coverage"])
        quality = SweepQuality(data["quality"]["status"], tuple(data["quality"]["flags"]))
        device = DeviceIdentity(**data["device"])
        error = data["error_classification"]
        return SpectrumSweep(
            started_at=datetime.fromisoformat(data["started_at"]),
            finished_at=datetime.fromisoformat(data["finished_at"]),
            duration_seconds=data["duration_seconds"], start_hz=data["start_hz"],
            stop_hz=data["stop_hz"], frequencies_hz=frequencies,
            bin_width_hz=data["bin_width_hz"], powers=powers, backend=data["backend"],
            receiver=data["receiver"], tuner=data["tuner"], status=data["status"],
            schema_version=data["schema_version"], sweep_id=data["sweep_id"],
            sequence=data["sequence"], correlation_id=data["correlation_id"],
            requested_profile=profile(data["requested_profile"]),
            actual_profile=profile(data["actual_profile"]), device=device,
            tool_version=data["tool_version"], coverage=coverage, quality=quality,
            error_classification=None if error is None else ErrorClassification(**error),
        )

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error as error:
            raise MeasurementPersistenceError("Could not close SQLite measurement store") from error


SQLiteSweepStore = SQLiteMeasurementSink
