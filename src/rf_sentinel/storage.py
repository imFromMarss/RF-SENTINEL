"""Durable SQLite storage for canonical spectrum sweeps."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import struct
from collections.abc import Iterator
from typing import Iterable
from uuid import uuid4

from rf_sentinel.acquisition import (DeviceIdentity, ErrorClassification,
                                     MeasurementReceipt, MeasurementSink,
                                     SpectrumSweep, SweepCoverage, SweepQuality,
                                     SweepProfileMetadata)
from rf_sentinel.errors import MeasurementPersistenceError


SCHEMA_VERSION = 2
SCHEMA_NAME = "spectrum-sweeps.v1"
_FLOAT64 = struct.Struct("<d")


@dataclass(frozen=True)
class IncidentRecord:
    incident_id: str
    timestamp: str
    component: str
    classification: str
    safe_message: str
    correlation_id: str | None
    recovery_result: str


def _pack(values: Iterable[float]) -> bytes:
    values = tuple(values)
    return struct.pack(f"<{len(values)}d", *values) if values else b""


def _unpack(blob: bytes | None, count: int, field: str, *, compact: bool = False):
    if blob is None:
        if count == 0:
            return ()
        raise ValueError(f"missing {field} payload")
    if len(blob) != count * _FLOAT64.size:
        raise ValueError(f"invalid {field} payload length")
    if not count:
        return ()
    if compact:
        from array import array
        values = array("d")
        values.frombytes(blob)
        return values
    return struct.unpack(f"<{count}d", blob)


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

    def __init__(self, path: str | Path, *, incident_retention: int = 1000):
        if type(incident_retention) is not int or not 1 <= incident_retention <= 100_000:
            raise ValueError("incident_retention must be between 1 and 100000")
        self.path = Path(path)
        self.incident_retention = incident_retention
        self.persisted_count = 0
        self.failed_count = 0
        self.last_persisted_sweep_at: str | None = None
        self.storage_error: str | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Acquisition persistence runs on AsyncMeasurementSink's writer
            # thread; the observer may use this same connection for metrics
            # and incidents on the application thread.
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._initialize()
        except (OSError, sqlite3.Error) as error:
            raise MeasurementPersistenceError("Could not open SQLite measurement store") from error

    def _initialize(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, SCHEMA_VERSION):
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
                    PRAGMA user_version = 2;
                """)
            with self._db:
                self._create_incidents_table()
        elif version == 1:
            with self._db:
                self._create_incidents_table()
                self._db.execute("PRAGMA user_version = 2")

    def _create_incidents_table(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY,
                timestamp_us INTEGER NOT NULL,
                component TEXT NOT NULL,
                classification TEXT NOT NULL,
                safe_message TEXT NOT NULL,
                correlation_id TEXT,
                recovery_result TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_incidents_timestamp ON incidents(timestamp_us);
            CREATE INDEX IF NOT EXISTS idx_incidents_correlation ON incidents(correlation_id);
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
            self.persisted_count += 1
            self.last_persisted_sweep_at = sweep.finished_at.isoformat()
            self.storage_error = None
        except (OSError, sqlite3.Error, TypeError, ValueError, struct.error) as error:
            self.failed_count += 1
            self.storage_error = "SQLite persistence failed"
            raise MeasurementPersistenceError("Could not persist spectrum sweep") from error
        return MeasurementReceipt(sweep.sweep_id, "persisted")

    def record_incident(self, *, component: str, classification: str,
                        safe_message: str, correlation_id: str | None,
                        recovery_result: str, timestamp: datetime | None = None) -> IncidentRecord:
        timestamp = timestamp or datetime.now().astimezone()
        record = IncidentRecord(str(uuid4()), timestamp.isoformat(), component, classification,
                                safe_message, correlation_id, recovery_result)
        try:
            with self._db:
                self._db.execute(
                    """INSERT INTO incidents
                    (incident_id, timestamp_us, component, classification, safe_message,
                     correlation_id, recovery_result) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (record.incident_id, _timestamp(timestamp), record.component,
                     record.classification, record.safe_message, record.correlation_id,
                     record.recovery_result),
                )
                self._db.execute(
                    """DELETE FROM incidents WHERE incident_id IN (
                    SELECT incident_id FROM incidents ORDER BY timestamp_us DESC, incident_id DESC
                    LIMIT -1 OFFSET ?)""", (self.incident_retention,))
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            self.storage_error = "SQLite incident history unavailable"
            raise MeasurementPersistenceError("Could not persist incident record") from error
        return record

    def query_incidents(self, limit: int | None = None) -> list[IncidentRecord]:
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 100_000):
            raise ValueError("incident limit must be between 1 and 100000")
        sql = ("SELECT incident_id, timestamp_us, component, classification, safe_message, "
               "correlation_id, recovery_result FROM incidents ORDER BY timestamp_us, incident_id")
        params = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        try:
            rows = self._db.execute(sql, params).fetchall()
        except sqlite3.Error as error:
            raise MeasurementPersistenceError("Could not read incident history") from error
        return [IncidentRecord(row[0], datetime.fromtimestamp(row[1] / 1_000_000, UTC).isoformat(),
                               row[2], row[3], row[4], row[5], row[6]) for row in rows]

    def storage_status(self) -> dict:
        try:
            db_size = self.path.stat().st_size
        except OSError:
            db_size = None
        return {"last_persisted_sweep_at": self.last_persisted_sweep_at,
                "persisted_sweeps": self.persisted_count,
                "failed_persists": self.failed_count,
                "sqlite_db_size_bytes": db_size,
                "storage_error": self.storage_error}

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
                "ORDER BY started_at_us, sweep_id", (start_us, end_us))
            return [self._decode(row) for row in rows]
        except (sqlite3.Error, TypeError, ValueError, KeyError, struct.error, json.JSONDecodeError) as error:
            raise MeasurementPersistenceError("Could not query spectrum sweeps") from error

    def _decode(self, row) -> SpectrumSweep:
        return _decode_sweep_row(row)

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error as error:
            raise MeasurementPersistenceError("Could not close SQLite measurement store") from error


class SQLiteSweepReader:
    """Read-only view of the durable sweep database.

    The reader opens SQLite in ``mode=ro`` and never initializes or mutates the
    database.  It is the boundary used by reporting, so report generation
    cannot accidentally start acquisition or block its writer.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise MeasurementPersistenceError("Could not open SQLite measurement store")
        try:
            self._db = sqlite3.connect(
                f"file:{self.path.absolute()}?mode=ro", uri=True,
            )
            self._db.execute("PRAGMA query_only=ON")
        except (OSError, sqlite3.Error) as error:
            raise MeasurementPersistenceError("Could not open SQLite measurement store") from error

    def query_sweeps(self, start: datetime, end: datetime) -> list[SpectrumSweep]:
        try:
            start_us, end_us = _timestamp(start), _timestamp(end)
            cursor = self._db.execute(
                "SELECT metadata_json, frequency_count, frequencies_blob, power_count, powers_blob "
                "FROM sweeps WHERE started_at_us >= ? AND started_at_us < ? "
                "ORDER BY started_at_us, sweep_id", (start_us, end_us))
            return [_decode_sweep_row(row) for row in cursor]
        except (sqlite3.Error, TypeError, ValueError, KeyError, struct.error, json.JSONDecodeError) as error:
            raise MeasurementPersistenceError("Could not query spectrum sweeps") from error

    def iter_sweeps(self, start: datetime, end: datetime) -> Iterator[SpectrumSweep]:
        """Yield report rows one at a time, keeping SQLite and decoded rows bounded."""
        try:
            start_us, end_us = _timestamp(start), _timestamp(end)
            cursor = self._db.execute(
                "SELECT metadata_json, frequency_count, frequencies_blob, power_count, powers_blob "
                "FROM sweeps WHERE started_at_us >= ? AND started_at_us < ? "
                "ORDER BY started_at_us, sweep_id", (start_us, end_us))
            while (row := cursor.fetchone()) is not None:
                yield _decode_sweep_row(row, compact=True)
        except (sqlite3.Error, TypeError, ValueError, KeyError, struct.error, json.JSONDecodeError) as error:
            raise MeasurementPersistenceError("Could not query spectrum sweeps") from error

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error as error:
            raise MeasurementPersistenceError("Could not close SQLite measurement store") from error


def _decode_sweep_row(row, *, compact: bool = False) -> SpectrumSweep:
    data = json.loads(row[0])
    frequencies = _unpack(row[2], row[1], "frequency", compact=compact)
    powers = _unpack(row[4], row[3], "power", compact=compact)

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

SQLiteSweepStore = SQLiteMeasurementSink
