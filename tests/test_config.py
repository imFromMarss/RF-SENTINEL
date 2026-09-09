import traceback
from pathlib import Path

import pytest

from rf_sentinel.config import Settings
from rf_sentinel.errors import ConfigurationError


def test_defaults():
    settings = Settings.from_env({})
    assert settings.report_interval_minutes == 30
    assert settings.rtl_device_index == 0
    assert settings.rtl_gain is None
    assert settings.data_dir == Path("runtime")
    assert settings.survey_low_hz == 24_000_000
    assert settings.survey_high_hz == 1_766_000_000
    assert settings.survey_bin_hz == 500_000
    assert settings.survey_integration_seconds == 60
    assert settings.survey_duration_seconds == 1800
    assert settings.acquisition_cadence_budget_seconds == 60
    assert settings.telegram_attempts == 3
    assert settings.survey_recovery_seconds == 60
    assert settings.incident_retention == 1000
    assert not settings.telegram_enabled


def test_environment_parsing():
    settings = Settings.from_env({
        "RF_SENTINEL_REPORT_INTERVAL_MINUTES": "5",
        "RF_SENTINEL_RTL_DEVICE_INDEX": "1",
        "RF_SENTINEL_RTL_GAIN": "20.7",
        "RF_SENTINEL_DATA_DIR": "data/surveys",
        "RF_SENTINEL_SURVEY_START_HZ": "25000000",
        "RF_SENTINEL_SURVEY_STOP_HZ": "1700000000",
        "RF_SENTINEL_SURVEY_BIN_HZ": "600000",
        "RF_SENTINEL_SURVEY_INTEGRATION_SECONDS": "90",
        "RF_SENTINEL_SURVEY_DURATION_SECONDS": "1800",
        "RF_SENTINEL_TELEGRAM_ATTEMPTS": "2",
        "RF_SENTINEL_TELEGRAM_BACKOFF_SECONDS": "1",
        "RF_SENTINEL_SURVEY_RECOVERY_DELAY_SECONDS": "30",
        "RF_SENTINEL_ACQUISITION_CADENCE_BUDGET_SECONDS": "75",
    })
    assert (settings.report_interval_minutes, settings.rtl_device_index) == (5, 1)
    assert settings.rtl_gain == 20.7
    assert settings.data_dir == Path("data/surveys")
    assert settings.survey_low_hz == 25_000_000
    assert settings.survey_high_hz == 1_700_000_000
    assert settings.survey_bin_hz == 600_000
    assert settings.survey_duration_seconds == 1800
    assert settings.survey_recovery_seconds == 30
    assert settings.acquisition_cadence_budget_seconds == 75


@pytest.mark.parametrize("name,value", [
    ("REPORT_INTERVAL_MINUTES", "0"), ("REPORT_INTERVAL_MINUTES", "1441"),
    ("REPORT_INTERVAL_MINUTES", "1.5"), ("RTL_DEVICE_INDEX", "-1"),
    ("RTL_DEVICE_INDEX", "256"), ("RTL_GAIN", "nan"), ("RTL_GAIN", "inf"),
    ("RTL_GAIN", "-1"), ("RTL_GAIN", "51"), ("DATA_DIR", ""),
    ("DATA_DIR", "a\0b"), ("TELEGRAM_CHAT_ID", "1"),
    ("SURVEY_START_HZ", "23000000"), ("SURVEY_STOP_HZ", "1766000001"),
    ("SURVEY_BIN_HZ", "100000"), ("SURVEY_INTEGRATION_SECONDS", "10"),
    ("SURVEY_DURATION_SECONDS", "1801"), ("TELEGRAM_ATTEMPTS", "0"),
    ("TELEGRAM_ATTEMPTS", "6"), ("TELEGRAM_BACKOFF_SECONDS", "301"),
    ("SURVEY_RECOVERY_DELAY_SECONDS", "0"), ("TIMEZONE", "Mars/Olympus"),
])
def test_invalid_configuration(name, value):
    with pytest.raises(ConfigurationError):
        Settings.from_env({"RF_SENTINEL_" + name: value})


def test_incident_retention_boundary_is_configurable():
    assert Settings.from_env({"RF_SENTINEL_INCIDENT_RETENTION": "25"}).incident_retention == 25
    with pytest.raises(ConfigurationError):
        Settings.from_env({"RF_SENTINEL_INCIDENT_RETENTION": "0"})


def test_legacy_recovery_setting_remains_supported():
    assert Settings.from_env({
        "RF_SENTINEL_SURVEY_RECOVERY_SECONDS": "17",
    }).survey_recovery_seconds == 17


def test_preferred_recovery_setting_overrides_legacy_name():
    assert Settings.from_env({
        "RF_SENTINEL_SURVEY_RECOVERY_DELAY_SECONDS": "23",
        "RF_SENTINEL_SURVEY_RECOVERY_SECONDS": "17",
    }).survey_recovery_seconds == 23


def test_sensitive_invalid_value_not_in_traceback():
    marker = "synthetic-sensitive-marker"
    with pytest.raises(ConfigurationError) as error:
        Settings.from_env({"RF_SENTINEL_RTL_GAIN": marker})
    assert marker not in "".join(traceback.format_exception(error.value))


def test_secret_representation_and_pair_validation():
    token = "0:" + "synthetic_dummy"
    settings = Settings(telegram_bot_token=token, telegram_chat_id="-1")
    assert settings.telegram_enabled
    assert token not in repr(settings)
    assert "synthetic_dummy" not in repr(settings)
    with pytest.raises(ConfigurationError):
        Settings(telegram_bot_token=token)
    with pytest.raises(ConfigurationError):
        Settings(telegram_bot_token=token, telegram_chat_id="bad\r\nheader")
