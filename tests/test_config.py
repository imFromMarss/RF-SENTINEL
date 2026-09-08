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
    assert not settings.telegram_enabled


def test_environment_parsing():
    settings = Settings.from_env({
        "RF_SENTINEL_REPORT_INTERVAL_MINUTES": "5",
        "RF_SENTINEL_RTL_DEVICE_INDEX": "1",
        "RF_SENTINEL_RTL_GAIN": "20.7",
        "RF_SENTINEL_DATA_DIR": "data/surveys",
    })
    assert (settings.report_interval_minutes, settings.rtl_device_index) == (5, 1)
    assert settings.rtl_gain == 20.7
    assert settings.data_dir == Path("data/surveys")


@pytest.mark.parametrize("name,value", [
    ("REPORT_INTERVAL_MINUTES", "0"), ("REPORT_INTERVAL_MINUTES", "1441"),
    ("REPORT_INTERVAL_MINUTES", "1.5"), ("RTL_DEVICE_INDEX", "-1"),
    ("RTL_DEVICE_INDEX", "256"), ("RTL_GAIN", "nan"), ("RTL_GAIN", "inf"),
    ("RTL_GAIN", "-1"), ("RTL_GAIN", "51"), ("DATA_DIR", ""),
    ("DATA_DIR", "a\0b"), ("TELEGRAM_CHAT_ID", "1"),
])
def test_invalid_configuration(name, value):
    with pytest.raises(ConfigurationError):
        Settings.from_env({"RF_SENTINEL_" + name: value})


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
