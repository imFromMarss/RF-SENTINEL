import copy
import json
import math
from dataclasses import FrozenInstanceError

import pytest

from rf_sentinel.calibration import (
    MODEL_TYPE,
    SCHEMA,
    CalibrationArtifact,
    CalibrationPoint,
    CWMeasurementProfile,
    InvalidCalibrationArtifactError,
    ReceiverIdentity,
    SourceDataset,
    SourcePointReference,
    calibration_artifact_identity,
    calibration_artifact_to_dict,
    calibration_point_identity,
    load_calibration_artifact,
    validate_calibration_artifact,
)
from rf_sentinel.checkpoint import canonical_point_identity


def artifact():
    raw, reference = -42.0, -30.0
    point_id = canonical_point_identity({
        "frequency_hz": 100_000_000,
        "raw_reference_level_db": raw.hex(),
        "reference_input_power_dbm": reference.hex(),
    })
    point = CalibrationPoint(
        point_id, 100_000_000, reference, raw, reference - raw, "accepted",
        SourcePointReference("raw-1", "raw-point"),
        SourcePointReference("ref-1", "ref-point"),
    )
    datasets = (
        SourceDataset("raw-1", "raw_characterization", "a" * 64),
        SourceDataset("ref-1", "input_power_reference", "b" * 64),
    )
    base = CalibrationArtifact(
        "", ReceiverIdentity("RTL-SDR", "Blog V4", "serial-1", "R820T2"),
        12.5, CWMeasurementProfile(2_400_000, 2048, 4, "hann"),
        {"type": MODEL_TYPE, "interpolation": "forbidden", "extrapolation": "forbidden"},
        {"reference_method": "bench generator", "requested_generator_level_dbm": -30.0,
         "source_datasets": ["raw-1", "ref-1"]}, datasets, (point,),
    )
    return CalibrationArtifact(calibration_artifact_identity(base), base.receiver,
                               base.observed_tuner_gain_db, base.measurement_profile,
                               base.model_metadata, base.provenance, base.datasets, base.points)


def test_valid_round_trip_and_identities(tmp_path):
    value = artifact()
    assert validate_calibration_artifact(value) == value
    assert calibration_point_identity(value.points[0]) == value.points[0].point_id
    payload = calibration_artifact_to_dict(value)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_calibration_artifact(path) == value


def test_models_are_immutable_and_nested_collections_are_tuples():
    value = artifact()
    assert isinstance(value.points, tuple) and isinstance(value.datasets, tuple)
    with pytest.raises(FrozenInstanceError):
        value.calibration_id = "x"
    with pytest.raises(TypeError):
        value.provenance["operator"] = "other"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_values_fail_closed(bad):
    value = artifact()
    broken = CalibrationArtifact(value.calibration_id, value.receiver, bad, value.measurement_profile,
                                 value.model_metadata, value.provenance, value.datasets, value.points)
    with pytest.raises(InvalidCalibrationArtifactError):
        validate_calibration_artifact(broken)


def test_duplicate_json_keys_are_rejected(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":"%s","schema_version":"%s"}' % (SCHEMA, SCHEMA), encoding="utf-8")
    with pytest.raises(InvalidCalibrationArtifactError):
        load_calibration_artifact(path)


def test_identity_changes_when_provenance_changes():
    value = artifact()
    changed = CalibrationArtifact("", value.receiver, value.observed_tuner_gain_db,
                                  value.measurement_profile, value.model_metadata,
                                  {"reference_method": "bench generator", "requested_generator_level_dbm": -29.0,
                                   "source_datasets": ["raw-1", "ref-1"]},
                                  value.datasets, value.points)
    assert calibration_artifact_identity(changed) != value.calibration_id


def payload():
    return calibration_artifact_to_dict(artifact())


def assert_invalid(value):
    with pytest.raises(InvalidCalibrationArtifactError):
        validate_calibration_artifact(value)


@pytest.mark.parametrize("field,value", [("reference_plane", "antenna"), ("quantity", "power"), ("unit", "W")])
def test_canonical_target_constants_are_strict(field, value):
    data = payload()
    data["target"][field] = value
    assert_invalid(data)


@pytest.mark.parametrize("field,value", [("quantity", "raw"), ("unit", "dBm")])
def test_canonical_raw_constants_are_strict(field, value):
    data = payload()
    data["raw_observable"][field] = value
    assert_invalid(data)


@pytest.mark.parametrize("field,value", [("type", "linear"), ("interpolation", "allowed"), ("extrapolation", "allowed")])
def test_canonical_model_constants_are_strict(field, value):
    data = payload()
    data["model"][field] = value
    assert_invalid(data)


@pytest.mark.parametrize("sources", [["raw-1", "missing"], ["raw-1", "raw-1"], ["raw-1"], ["ref-1"]])
def test_provenance_sources_must_resolve_be_unique_and_complete(sources):
    data = payload()
    data["provenance"]["source_datasets"] = sources
    assert_invalid(data)


@pytest.mark.parametrize("change", [
    lambda p: p["provenance"].pop("source_datasets"),
    lambda p: p["provenance"].update(extra=True),
])
def test_provenance_schema_is_strict(change):
    data = payload()
    change(data)
    assert_invalid(data)


def test_provenance_ids_resolve_and_generator_level_is_provenance_only():
    value = artifact()
    assert value.provenance["source_datasets"] == ("raw-1", "ref-1")
    changed = CalibrationArtifact(
        "", value.receiver, value.observed_tuner_gain_db, value.measurement_profile,
        value.model_metadata,
        {**value.provenance, "requested_generator_level_dbm": -29.0},
        value.datasets, value.points,
    )
    assert changed.points[0].reference_input_power_dbm == value.points[0].reference_input_power_dbm
    assert changed.points[0].correction_db == value.points[0].correction_db
    assert calibration_artifact_identity(changed) != value.calibration_id


@pytest.mark.parametrize("value", [" ", "unknown", "n/a", "na", "none", "null", "unset", "tbd"])
def test_receiver_identifiers_are_strict(value):
    data = payload()
    data["receiver"]["model"] = value
    assert_invalid(data)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), float("-inf"), 10 ** 10000],
                         ids=["bool", "nan", "posinf", "neginf", "oversized-int"])
def test_programmatic_numeric_fields_fail_with_calibration_error(value):
    data = payload()
    data["observed_tuner_gain_db"] = value
    assert_invalid(data)


@pytest.mark.parametrize("value", [[1], {}, None])
def test_wrong_root_container_type_is_calibration_error(value):
    assert_invalid(value)


def test_non_string_root_and_nested_keys_are_calibration_errors():
    data = payload()
    data[1] = "bad"
    assert_invalid(data)
    data = payload()
    data["receiver"][1] = "bad"
    assert_invalid(data)


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
def test_json_nonfinite_numbers_fail_closed(tmp_path, text):
    data = json.dumps(payload()).replace("12.5", text, 1)
    path = tmp_path / "invalid.json"
    path.write_text(data, encoding="utf-8")
    with pytest.raises(InvalidCalibrationArtifactError):
        load_calibration_artifact(path)


def test_json_numeric_overflow_fails_closed(tmp_path):
    data = json.dumps(payload()).replace("12.5", "1e400", 1)
    path = tmp_path / "overflow.json"
    path.write_text(data, encoding="utf-8")
    with pytest.raises(InvalidCalibrationArtifactError):
        load_calibration_artifact(path)


def test_serialization_is_fresh_json_shaped_and_round_trips():
    value = artifact()
    first = calibration_artifact_to_dict(value)
    first["provenance"]["source_datasets"].append("mutated")
    assert "mutated" not in value.provenance["source_datasets"]
    assert json.loads(json.dumps(calibration_artifact_to_dict(value))) == calibration_artifact_to_dict(value)


def test_golden_point_and_artifact_identities():
    value = artifact()
    assert value.points[0].point_id == (
        '{"frequency_hz":100000000,"raw_reference_level_db":"-0x1.5000000000000p+5",'
        '"reference_input_power_dbm":"-0x1.e000000000000p+4"}'
    )
    assert value.calibration_id == "sha256:e14edb03cddc87405ec403896d63d335b8ad9fa878ea7db7bc03a03ccd7e2938"


def test_dataset_order_does_not_change_artifact_identity():
    original = payload()
    changed = payload()
    changed["source_datasets"] = list(reversed(changed["source_datasets"]))
    assert validate_calibration_artifact(changed).calibration_id == original["calibration_id"]


def test_point_order_does_not_change_artifact_identity():
    original = payload()
    changed = payload()
    changed["points"] = list(reversed(changed["points"]))
    assert validate_calibration_artifact(changed).calibration_id == original["calibration_id"]


@pytest.mark.parametrize("field", ["point_id", "calibration_id"])
def test_stored_identity_corruption_is_rejected(field):
    data = payload()
    if field == "point_id":
        data["points"][0]["point_id"] = "corrupt"
    else:
        data["calibration_id"] = "corrupt"
    assert_invalid(data)


def test_dataset_integrity_and_point_source_references():
    cases = []
    data = payload(); data["source_datasets"][0]["dataset_id"] = "raw-1"; cases.append(data)
    data = payload(); data["source_datasets"][0]["sha256"] = "a" * 64; cases.append(data)
    data = payload(); data["source_datasets"][0]["role"] = "other"; cases.append(data)
    data = payload(); data["source_datasets"] = [data["source_datasets"][0]]; cases.append(data)
    data = payload(); data["source_datasets"] = [data["source_datasets"][1]]; cases.append(data)
    data = payload(); data["points"][0]["raw_source"]["dataset_id"] = "missing"; cases.append(data)
    data = payload(); data["points"][0]["reference_source"]["dataset_id"] = "missing"; cases.append(data)
    data = payload(); data["points"][0]["raw_source"]["dataset_id"] = "ref-1"; cases.append(data)
    data = payload(); data["points"][0]["reference_source"]["dataset_id"] = "raw-1"; cases.append(data)
    for invalid in cases:
        assert_invalid(invalid)


def test_duplicate_point_id_is_rejected():
    data = payload()
    data["points"].append(copy.deepcopy(data["points"][0]))
    assert_invalid(data)


def test_duplicate_frequency_with_distinct_point_id_is_rejected():
    data = payload()
    second = copy.deepcopy(data["points"][0])
    second["raw_reference_level_db"] = -41.0
    second["correction_db"] = 11.0
    second["point_id"] = canonical_point_identity({
        "frequency_hz": second["frequency_hz"],
        "raw_reference_level_db": float(second["raw_reference_level_db"]).hex(),
        "reference_input_power_dbm": float(second["reference_input_power_dbm"]).hex(),
    })
    data["points"].append(second)
    assert_invalid(data)


def _artifact_payload_with_correction(correction):
    value = artifact()
    point = CalibrationPoint(
        value.points[0].point_id, value.points[0].frequency_hz,
        value.points[0].reference_input_power_dbm, value.points[0].raw_reference_level_db,
        correction, value.points[0].quality, value.points[0].raw_source, value.points[0].reference_source,
    )
    changed = CalibrationArtifact(
        "", value.receiver, value.observed_tuner_gain_db, value.measurement_profile,
        value.model_metadata, value.provenance, value.datasets, (point,),
    )
    changed = CalibrationArtifact(
        calibration_artifact_identity(changed), changed.receiver, changed.observed_tuner_gain_db,
        changed.measurement_profile, changed.model_metadata, changed.provenance,
        changed.datasets, changed.points,
    )
    return calibration_artifact_to_dict(changed)


@pytest.mark.parametrize("steps", [0, 1, -1, 2, -2], ids=["exact", "plus-1", "minus-1", "plus-2", "minus-2"])
def test_correction_accepts_exact_and_two_binary64_steps(steps):
    stored = 12.0
    direction = math.inf if steps >= 0 else -math.inf
    for _ in range(abs(steps)):
        stored = math.nextafter(stored, direction)
    assert validate_calibration_artifact(_artifact_payload_with_correction(stored)).points[0].correction_db == stored


@pytest.mark.parametrize("steps", [3, -3], ids=["plus-3", "minus-3"])
def test_correction_rejects_three_binary64_steps(steps):
    stored = 12.0
    direction = math.inf if steps > 0 else -math.inf
    for _ in range(abs(steps)):
        stored = math.nextafter(stored, direction)
    assert_invalid(_artifact_payload_with_correction(stored))


@pytest.mark.parametrize("reference,raw", [
    (1.0, math.nextafter(1.0, 0.0)),
    (-1.0, 0.0),
    (math.nextafter(0.0, 1.0), 0.0),
    (0.0, 0.0),
    (-0.0, 0.0),
])
def test_correction_binary64_boundaries(reference, raw):
    value = artifact()
    point_id = canonical_point_identity({
        "frequency_hz": 100_000_000,
        "raw_reference_level_db": float(raw).hex(),
        "reference_input_power_dbm": float(reference).hex(),
    })
    point = CalibrationPoint(point_id, 100_000_000, reference, raw, reference - raw, "accepted",
                             value.points[0].raw_source, value.points[0].reference_source)
    candidate = CalibrationArtifact("", value.receiver, value.observed_tuner_gain_db, value.measurement_profile,
                                    value.model_metadata, value.provenance, value.datasets, (point,))
    candidate = CalibrationArtifact(calibration_artifact_identity(candidate), candidate.receiver, candidate.observed_tuner_gain_db,
                                    candidate.measurement_profile, candidate.model_metadata, candidate.provenance,
                                    candidate.datasets, candidate.points)
    assert validate_calibration_artifact(calibration_artifact_to_dict(candidate)).points[0].correction_db == reference - raw


def test_deep_alias_isolation_for_input_and_serialized_output():
    source = payload()
    validated = validate_calibration_artifact(source)
    baseline = calibration_artifact_to_dict(validated)
    source["provenance"]["source_datasets"][0] = "changed"
    source["source_datasets"][0]["dataset_id"] = "changed"
    source["points"][0]["raw_source"]["point_id"] = "changed"
    assert calibration_artifact_to_dict(validated) == baseline
    serialized = calibration_artifact_to_dict(validated)
    serialized["provenance"]["source_datasets"].append("changed")
    serialized["source_datasets"][0]["sha256"] = "c" * 64
    serialized["points"][0]["reference_source"]["dataset_id"] = "changed"
    assert calibration_artifact_to_dict(validated) == baseline


def _assert_json_shaped(value):
    assert type(value) in (dict, list, str, int, float, bool) or value is None
    if type(value) is dict:
        assert all(type(key) is str for key in value)
        for item in value.values():
            _assert_json_shaped(item)
    elif type(value) is list:
        for item in value:
            _assert_json_shaped(item)


def test_recursive_json_shaped_round_trip_preserves_semantics():
    value = artifact()
    serialized = calibration_artifact_to_dict(value)
    _assert_json_shaped(serialized)
    round_tripped = validate_calibration_artifact(json.loads(json.dumps(serialized)))
    assert round_tripped.calibration_id == value.calibration_id
    assert [p.point_id for p in round_tripped.points] == [p.point_id for p in value.points]
    assert round_tripped == value
