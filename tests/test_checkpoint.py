import json

import pytest

from rf_sentinel.checkpoint import (
    atomic_write_json,
    canonical_point_identity,
    validate_checkpoint_envelope,
    validate_checkpoint_identities,
)
from rf_sentinel.cw_frequency_accuracy import FrequencyAccuracyPoint
from rf_sentinel.cw_matrix import MatrixPoint


def test_matrix_identity_golden_exact() -> None:
    point = MatrixPoint(1, 433_000_000, 12.5, 100_000)

    assert point.checkpoint_identity == (
        '{"requested_bin_hz":100000,"requested_frequency_hz":433000000,'
        '"requested_gain_db_hex":"0x1.9000000000000p+3"}'
    )


def test_close_gains_have_different_identities() -> None:
    first = MatrixPoint(1, 433_000_000, 12.5, 100_000)
    second = MatrixPoint(2, 433_000_000, 12.500000000000002, 100_000)

    assert first.checkpoint_identity != second.checkpoint_identity


def test_frequency_accuracy_identity_golden_exact() -> None:
    point = FrequencyAccuracyPoint(1, 433_000_000, -250_000, 2, "offset")

    assert point.checkpoint_identity == (
        '{"center_offset_hz":-250000,"repeat":2,'
        '"requested_cw_frequency_hz":433000000,'
        '"requested_tuner_center_hz":432750000,"tuning_mode":"offset"}'
    )


def test_valid_int_string_identity_mapping_is_exact() -> None:
    assert canonical_point_identity({"frequency_hz": 433_000_000, "mode": "normal"}) == (
        '{"frequency_hz":433000000,"mode":"normal"}'
    )


@pytest.mark.parametrize("value", [433_000_000.0, True, None, [], {}, (1, 2)])
def test_invalid_identity_values_are_rejected(value) -> None:
    with pytest.raises(TypeError):
        canonical_point_identity({"frequency_hz": value})


def test_non_string_identity_key_is_rejected() -> None:
    with pytest.raises(TypeError, match="keys must be strings"):
        canonical_point_identity({433_000_000: "normal"})


def test_checkpoint_envelope_requires_exact_schema_configuration_and_points() -> None:
    configuration = {"device": 0, "modes": ["normal"]}
    payload = {
        "schema_version": "example.v1",
        "configuration": configuration,
        "points": [{"checkpoint_identity": "one"}],
    }

    assert validate_checkpoint_envelope(
        payload, expected_schema_version="example.v1",
        expected_configuration=configuration) == payload["points"]

    for corrupted in (
        {**payload, "schema_version": "example.v2"},
        {**payload, "configuration": {"device": False, "modes": ["normal"]}},
        {key: value for key, value in payload.items() if key != "points"},
    ):
        with pytest.raises(ValueError):
            validate_checkpoint_envelope(
                corrupted, expected_schema_version="example.v1",
                expected_configuration=configuration)


def test_checkpoint_identity_validation_returns_exact_stored_set() -> None:
    assert validate_checkpoint_identities(
        planned_identities=("one", "two"),
        stored_identities=("two",),
        recomputed_identities=("two",),
    ) == {"two"}


@pytest.mark.parametrize("planned,stored,recomputed", [
    (("one", "one"), (), ()),
    (("one",), ("one", "one"), ("one", "one")),
    (("one",), ("foreign",), ("foreign",)),
    (("one", "two"), ("one",), ("two",)),
])
def test_checkpoint_identity_validation_rejects_invalid_sets(
        planned, stored, recomputed) -> None:
    with pytest.raises(ValueError):
        validate_checkpoint_identities(
            planned_identities=planned,
            stored_identities=stored,
            recomputed_identities=recomputed)


def test_atomic_write_json_preserves_expected_json(tmp_path) -> None:
    path = tmp_path / "results.json"
    payload = {"message": "привіт", "points": [1, 2]}

    atomic_write_json(path, payload)

    assert path.read_text(encoding="utf-8") == (
        '{\n  "message": "привіт",\n  "points": [\n    1,\n    2\n  ]\n}\n'
    )
    assert json.loads(path.read_text(encoding="utf-8")) == payload


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_atomic_write_json_failure_preserves_previous_checkpoint(tmp_path, monkeypatch, failure) -> None:
    path = tmp_path / "results.json"
    previous = '{\n  "status": "valid"\n}\n'
    path.write_text(previous, encoding="utf-8")

    if failure == "write":
        original_write_text = type(path).write_text

        def fail_temp_write(self, data, *args, **kwargs):
            if self.name == "results.json.tmp":
                raise OSError("write failed")
            return original_write_text(self, data, *args, **kwargs)

        monkeypatch.setattr(type(path), "write_text", fail_temp_write)
    else:
        original_replace = type(path).replace

        def fail_replace(self, target):
            if self.name == "results.json.tmp":
                raise OSError("replace failed")
            return original_replace(self, target)

        monkeypatch.setattr(type(path), "replace", fail_replace)

    with pytest.raises(OSError):
        atomic_write_json(path, {"status": "new"})

    assert path.read_text(encoding="utf-8") == previous
