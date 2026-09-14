"""Immutable, self-identifying CW calibration artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .checkpoint import canonical_point_identity

SCHEMA = "rf-sentinel-cw-amplitude-calibration.v1"
SCHEMA_VERSION = SCHEMA
CALIBRATION_SCHEMA = SCHEMA
MODEL_TYPE = "additive"
QUALITY_ACCEPTED = "accepted"
_ROLES = {"raw_characterization", "input_power_reference"}
_DIGEST = __import__("re").compile(r"^[0-9a-f]{64}$")


class CalibrationError(ValueError):
    """Base class for calibration artifact failures."""

    def __init__(self, message: str, *, code: str = "invalid_calibration", path: str | None = None):
        super().__init__(message)
        self.code = code
        self.path = path


class InvalidCalibrationArtifactError(CalibrationError):
    """A calibration artifact is malformed or violates its contract."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise InvalidCalibrationArtifactError("JSON object keys must be strings", code="wrong_type", path="$")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if type(value) is str or type(value) is bool or value is None or type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise InvalidCalibrationArtifactError("unsupported JSON value", code="wrong_type", path="$")


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return _freeze(value)


@dataclass(frozen=True)
class ReceiverIdentity:
    manufacturer: str
    model: str
    serial_number: str
    tuner: str


@dataclass(frozen=True)
class CWMeasurementProfile:
    sample_rate_hz: int
    fft_size: int
    averaging_count: int
    window: str


@dataclass(frozen=True)
class SourceDataset:
    dataset_id: str
    role: str
    sha256: str


@dataclass(frozen=True)
class SourcePointReference:
    dataset_id: str
    point_id: str


@dataclass(frozen=True)
class CalibrationPoint:
    point_id: str
    frequency_hz: int
    reference_input_power_dbm: float
    raw_reference_level_db: float
    correction_db: float
    quality: str
    raw_source: SourcePointReference
    reference_source: SourcePointReference

    @property
    def computed_identity(self) -> str:
        return canonical_point_identity({
            "frequency_hz": self.frequency_hz,
            "raw_reference_level_db": self.raw_reference_level_db.hex(),
            "reference_input_power_dbm": self.reference_input_power_dbm.hex(),
        })


@dataclass(frozen=True)
class CalibrationArtifact:
    calibration_id: str
    receiver: ReceiverIdentity
    observed_tuner_gain_db: float
    measurement_profile: CWMeasurementProfile
    model_metadata: Mapping[str, Any]
    provenance: Mapping[str, Any]
    datasets: tuple[SourceDataset, ...]
    points: tuple[CalibrationPoint, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_metadata", _frozen_mapping(self.model_metadata))
        object.__setattr__(self, "provenance", _frozen_mapping(self.provenance))
        object.__setattr__(self, "datasets", tuple(sorted(self.datasets, key=lambda d: (d.role, d.dataset_id))))
        object.__setattr__(self, "points", tuple(sorted(self.points, key=lambda p: p.frequency_hz)))

    @property
    def applied_tuner_gain_db(self) -> float:
        return self.observed_tuner_gain_db


def calibration_point_identity(point: CalibrationPoint) -> str:
    return point.computed_identity


def _fail(message: str, code: str, path: str) -> None:
    raise InvalidCalibrationArtifactError(message, code=code, path=path)


def _object(value: object, path: str) -> dict[str, Any]:
    if type(value) is not dict:
        _fail("expected JSON object", "wrong_type", path)
    return value  # type: ignore[return-value]


def _keys(value: dict[str, Any], required: set[str], allowed: set[str], path: str) -> None:
    if any(type(key) is not str for key in value):
        _fail("JSON object keys must be strings", "wrong_type", path)
    keys = set(value)
    unknown = keys - allowed
    missing = required - keys
    if unknown:
        _fail("unknown key: " + sorted(unknown)[0], "unknown_key", path)
    if missing:
        _fail("missing key: " + sorted(missing)[0], "missing_key", path)


def _str(value: object, path: str, *, nonempty: bool = True) -> str:
    if type(value) is not str or (nonempty and not value.strip()):
        _fail("expected non-empty string", "wrong_type", path)
    return value


def _known_str(value: object, path: str) -> str:
    result = _str(value, path)
    if result.strip().casefold() in {"unknown", "n/a", "na", "none", "null", "unset", "tbd"}:
        _fail("unknown value is not allowed", "unknown_value", path)
    return result


def _int(value: object, path: str, *, positive: bool = False) -> int:
    if type(value) is not int or (positive and value <= 0):
        _fail("expected integer", "wrong_type", path)
    return value


def _finite(value: object, path: str) -> float:
    if type(value) not in (int, float):
        _fail("expected finite number", "wrong_numeric", path)
    try:
        result = float(value)
    except (OverflowError, ValueError, TypeError) as exc:
        raise InvalidCalibrationArtifactError(
            "number is not representable as binary64", code="wrong_numeric", path=path
        ) from exc
    if not math.isfinite(result):
        _fail("expected finite number", "wrong_numeric", path)
    return result


def _canonical(value: object) -> object:
    if isinstance(value, float):
        return value.hex()
    if isinstance(value, Mapping):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _within_binary64_steps(expected: float, stored: float, steps: int = 2) -> bool:
    """Compare values by adjacent binary64 representations, including boundaries."""
    if not (math.isfinite(expected) and math.isfinite(stored)):
        return False
    if expected == stored:  # includes +0.0 and -0.0
        return True
    lower, upper = stored, stored
    for _ in range(steps):
        lower = math.nextafter(lower, -math.inf)
        upper = math.nextafter(upper, math.inf)
    return lower <= expected <= upper


def _compact(value: object) -> str:
    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _artifact_payload(artifact: CalibrationArtifact) -> dict[str, Any]:
    payload = calibration_artifact_to_dict(artifact).copy()
    payload.pop("calibration_id", None)
    return payload


def _artifact_identity(artifact: CalibrationArtifact) -> str:
    return "sha256:" + hashlib.sha256(_compact(_artifact_payload(artifact)).encode("utf-8")).hexdigest()


def calibration_artifact_identity(artifact: CalibrationArtifact) -> str:
    """Recompute the artifact identity without using its stored ID."""
    return _artifact_identity(artifact)


def _parse_reference(value: object, path: str) -> SourcePointReference:
    data = _object(value, path)
    _keys(data, {"dataset_id", "point_id"}, {"dataset_id", "point_id"}, path)
    return SourcePointReference(_str(data["dataset_id"], path + ".dataset_id"), _str(data["point_id"], path + ".point_id"))


def _parse_artifact(data: object) -> CalibrationArtifact:
    root = _object(data, "$")
    allowed = {"schema_version", "calibration_id", "target", "raw_observable", "receiver", "observed_tuner_gain_db", "measurement_profile", "model", "provenance", "source_datasets", "points"}
    _keys(root, allowed, allowed, "$")
    if root["schema_version"] != SCHEMA:
        _fail("schema does not match", "wrong_schema", "$.schema_version")
    target = _object(root["target"], "$.target")
    _keys(target, {"quantity", "unit", "reference_plane"}, {"quantity", "unit", "reference_plane"}, "$.target")
    if target != {"quantity": "estimated_cw_input_power", "unit": "dBm", "reference_plane": "rtl_sdr_rf_input"}:
        _fail("target constants do not match", "wrong_constants", "$.target")
    raw = _object(root["raw_observable"], "$.raw_observable")
    _keys(raw, {"quantity", "unit"}, {"quantity", "unit"}, "$.raw_observable")
    if raw != {"quantity": "raw_reference_level", "unit": "dB"}:
        _fail("raw observable constants do not match", "wrong_constants", "$.raw_observable")
    receiver = _object(root["receiver"], "$.receiver")
    _keys(receiver, {"manufacturer", "model", "serial_number", "tuner"}, {"manufacturer", "model", "serial_number", "tuner"}, "$.receiver")
    receiver_obj = ReceiverIdentity(*[_known_str(receiver[k], "$.receiver." + k) for k in ("manufacturer", "model", "serial_number", "tuner")])
    gain = _finite(root["observed_tuner_gain_db"], "$.observed_tuner_gain_db")
    profile = _object(root["measurement_profile"], "$.measurement_profile")
    _keys(profile, {"sample_rate_hz", "fft_size", "averaging_count", "window"}, {"sample_rate_hz", "fft_size", "averaging_count", "window"}, "$.measurement_profile")
    profile_obj = CWMeasurementProfile(_int(profile["sample_rate_hz"], "$.measurement_profile.sample_rate_hz", positive=True), _int(profile["fft_size"], "$.measurement_profile.fft_size", positive=True), _int(profile["averaging_count"], "$.measurement_profile.averaging_count", positive=True), _str(profile["window"], "$.measurement_profile.window"))
    model = _object(root["model"], "$.model")
    _keys(model, {"type", "interpolation", "extrapolation"}, {"type", "interpolation", "extrapolation"}, "$.model")
    if model != {"type": MODEL_TYPE, "interpolation": "forbidden", "extrapolation": "forbidden"}:
        _fail("model constants do not match", "wrong_constants", "$.model")
    provenance = _object(root["provenance"], "$.provenance")
    _keys(provenance, {"reference_method", "requested_generator_level_dbm", "source_datasets"},
          {"reference_method", "requested_generator_level_dbm", "source_datasets"}, "$.provenance")
    _str(provenance["reference_method"], "$.provenance.reference_method")
    _finite(provenance["requested_generator_level_dbm"], "$.provenance.requested_generator_level_dbm")
    provenance_sources = provenance["source_datasets"]
    if type(provenance_sources) not in (list, tuple) or not provenance_sources:
        _fail("source_datasets must be a non-empty sequence", "wrong_type", "$.provenance.source_datasets")
    for i, source_id in enumerate(provenance_sources):
        _str(source_id, f"$.provenance.source_datasets[{i}]")
    datasets_data = root["source_datasets"]
    if type(datasets_data) is not list:
        _fail("source_datasets must be a list", "wrong_type", "$.source_datasets")
    datasets = []
    for i, item in enumerate(datasets_data):
        d = _object(item, f"$.source_datasets[{i}]")
        _keys(d, {"dataset_id", "role", "sha256"}, {"dataset_id", "role", "sha256"}, f"$.source_datasets[{i}]")
        ds = SourceDataset(_str(d["dataset_id"], f"$.source_datasets[{i}].dataset_id"), _str(d["role"], f"$.source_datasets[{i}].role"), _str(d["sha256"], f"$.source_datasets[{i}].sha256"))
        if ds.role not in _ROLES:
            _fail("unknown dataset role", "unknown_dataset_role", f"$.source_datasets[{i}].role")
        if not _DIGEST.fullmatch(ds.sha256):
            _fail("invalid SHA-256 digest", "invalid_digest", f"$.source_datasets[{i}].sha256")
        datasets.append(ds)
    if len({d.dataset_id for d in datasets}) != len(datasets):
        _fail("duplicate dataset ID", "duplicate_dataset_id", "$.source_datasets")
    if not _ROLES <= {d.role for d in datasets}:
        _fail("required source role is missing", "missing_source_role", "$.source_datasets")
    if any(sum(d.role == role for d in datasets) != 1 for role in _ROLES):
        _fail("each required source role must occur exactly once", "duplicate_source_role", "$.source_datasets")
    raw_dataset = next(d for d in datasets if d.role == "raw_characterization")
    reference_dataset = next(d for d in datasets if d.role == "input_power_reference")
    if raw_dataset.dataset_id == reference_dataset.dataset_id or raw_dataset.sha256 == reference_dataset.sha256:
        _fail("required source datasets must be independent", "non_independent_sources", "$.source_datasets")
    provenance_ids = list(provenance_sources)
    if len(set(provenance_ids)) != len(provenance_ids):
        _fail("duplicate provenance dataset ID", "duplicate_provenance_dataset_id", "$.provenance.source_datasets")
    declared_ids = {d.dataset_id for d in datasets}
    unresolved = [dataset_id for dataset_id in provenance_ids if dataset_id not in declared_ids]
    if unresolved:
        _fail("provenance dataset does not resolve", "unresolved_provenance_source", "$.provenance.source_datasets")
    if set(provenance_ids) != declared_ids:
        _fail("provenance must name every source dataset", "incomplete_provenance", "$.provenance.source_datasets")
    points_data = root["points"]
    if type(points_data) is not list or not points_data:
        _fail("points must be a non-empty list", "empty_points", "$.points")
    points = []
    dataset_by_id = {d.dataset_id: d for d in datasets}
    for i, item in enumerate(points_data):
        pth = f"$.points[{i}]"; p = _object(item, pth)
        fields = {"point_id", "frequency_hz", "reference_input_power_dbm", "raw_reference_level_db", "correction_db", "quality", "raw_source", "reference_source"}
        _keys(p, fields, fields, pth)
        point = CalibrationPoint(_str(p["point_id"], pth + ".point_id"), _int(p["frequency_hz"], pth + ".frequency_hz"), _finite(p["reference_input_power_dbm"], pth + ".reference_input_power_dbm"), _finite(p["raw_reference_level_db"], pth + ".raw_reference_level_db"), _finite(p["correction_db"], pth + ".correction_db"), _str(p["quality"], pth + ".quality"), _parse_reference(p["raw_source"], pth + ".raw_source"), _parse_reference(p["reference_source"], pth + ".reference_source"))
        if point.quality != QUALITY_ACCEPTED: _fail("point quality is not accepted", "invalid_quality", pth + ".quality")
        for ref, role in ((point.raw_source, "raw_characterization"), (point.reference_source, "input_power_reference")):
            ds = dataset_by_id.get(ref.dataset_id)
            if ds is None: _fail("unresolved source reference", "unresolved_source", pth)
            if ds.role != role: _fail("source reference has wrong role", "wrong_source_role", pth)
        if point.point_id != point.computed_identity: _fail("point identity mismatch", "point_identity_mismatch", pth + ".point_id")
        expected_correction = point.reference_input_power_dbm - point.raw_reference_level_db
        if not _within_binary64_steps(expected_correction, point.correction_db): _fail("correction arithmetic mismatch", "correction_mismatch", pth + ".correction_db")
        points.append(point)
    if len({p.point_id for p in points}) != len(points): _fail("duplicate point ID", "duplicate_point_id", "$.points")
    if len({p.frequency_hz for p in points}) != len(points): _fail("duplicate frequency", "duplicate_frequency", "$.points")
    artifact = CalibrationArtifact(_str(root["calibration_id"], "$.calibration_id"), receiver_obj, gain, profile_obj, model, provenance, tuple(sorted(datasets, key=lambda d: (d.role, d.dataset_id))), tuple(sorted(points, key=lambda p: p.frequency_hz)))
    expected = _artifact_identity(artifact)
    if artifact.calibration_id != expected: _fail("artifact identity mismatch", "artifact_identity_mismatch", "$.calibration_id")
    return artifact


def calibration_artifact_to_dict(artifact: CalibrationArtifact) -> dict[str, Any]:
    datasets = sorted(artifact.datasets, key=lambda d: (d.role, d.dataset_id))
    points = sorted(artifact.points, key=lambda p: p.frequency_hz)
    result = {"schema_version": SCHEMA, "calibration_id": artifact.calibration_id, "target": {"quantity": "estimated_cw_input_power", "unit": "dBm", "reference_plane": "rtl_sdr_rf_input"}, "raw_observable": {"quantity": "raw_reference_level", "unit": "dB"}, "receiver": {"manufacturer": artifact.receiver.manufacturer, "model": artifact.receiver.model, "serial_number": artifact.receiver.serial_number, "tuner": artifact.receiver.tuner}, "observed_tuner_gain_db": artifact.observed_tuner_gain_db, "measurement_profile": {"sample_rate_hz": artifact.measurement_profile.sample_rate_hz, "fft_size": artifact.measurement_profile.fft_size, "averaging_count": artifact.measurement_profile.averaging_count, "window": artifact.measurement_profile.window}, "model": artifact.model_metadata, "provenance": artifact.provenance, "source_datasets": [{"dataset_id": d.dataset_id, "role": d.role, "sha256": d.sha256} for d in datasets], "points": [{"point_id": p.point_id, "frequency_hz": p.frequency_hz, "reference_input_power_dbm": p.reference_input_power_dbm, "raw_reference_level_db": p.raw_reference_level_db, "correction_db": p.correction_db, "quality": p.quality, "raw_source": {"dataset_id": p.raw_source.dataset_id, "point_id": p.raw_source.point_id}, "reference_source": {"dataset_id": p.reference_source.dataset_id, "point_id": p.reference_source.point_id}} for p in points]}
    return _json_copy(result, "$")


def _json_copy(value: Any, path: str) -> Any:
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            _fail("JSON object keys must be strings", "wrong_type", path)
        return {key: _json_copy(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_copy(item, f"{path}[{i}]") for i, item in enumerate(value)]
    if type(value) in (str, bool, int) or value is None:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    _fail("unsupported JSON value", "wrong_type", path)


def validate_calibration_artifact(artifact: CalibrationArtifact | Mapping[str, Any]) -> CalibrationArtifact:
    """Validate either a model instance or its JSON-shaped dictionary."""
    if isinstance(artifact, Mapping):
        try:
            return _parse_artifact(dict(artifact))
        except InvalidCalibrationArtifactError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise InvalidCalibrationArtifactError(
                "malformed calibration artifact payload", code="invalid_calibration", path="$"
            ) from exc
    if not isinstance(artifact, CalibrationArtifact):
        _fail("expected calibration artifact", "wrong_type", "$")
    return _parse_artifact(calibration_artifact_to_dict(artifact))


def calibration_artifact_from_dict(payload: Mapping[str, Any]) -> CalibrationArtifact:
    return validate_calibration_artifact(payload)


def load_calibration_artifact(path: Path) -> CalibrationArtifact:
    def reject_constant(value: str) -> Any:
        raise ValueError("non-finite JSON number")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject_constant, object_pairs_hook=lambda pairs: _unique_object(pairs))
    except OSError:
        raise
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise InvalidCalibrationArtifactError(str(exc), code="invalid_json", path="$") from exc
    return _parse_artifact(data)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
