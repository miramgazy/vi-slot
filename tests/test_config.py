import json
import os
import pytest
from config import ConfigError, load_config, mask_secret


def get_valid_env():
    return {
        "BROWSERLESS_WS": "ws://browserless:3000/chromium?token=test-token",
        "OPENAI_API_KEY": "sk-proj-1234567890abcdef",
        "TG_BOT_TOKEN": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        "TG_CHAT_IDS": "111222, 333444, 555666",
        "MAIN_EMAIL": "main@example.com",
        "MAIN_PASSWORD": "SecretPassword123!",
        "DONOR_ACCOUNTS": json.dumps([
            {"email": "d1@example.com", "password": "pass1"},
            {"email": "d2@example.com", "password": "pass2"}
        ]),
        "USER_DATA_JSON": json.dumps({
            "first_name": "Test",
            "last_name": "User",
            "nationality": "Kazakhstan"
        }),
    }


def test_load_config_valid(monkeypatch):
    env = get_valid_env()
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    cfg = load_config(env_file=None)
    assert cfg.browserless_ws == "ws://browserless:3000/chromium?token=test-token"
    assert cfg.openai_api_key == "sk-proj-1234567890abcdef"
    assert cfg.tg_chat_ids == ["111222", "333444", "555666"]
    assert len(cfg.donor_accounts) == 2
    assert cfg.donor_accounts[0].email == "d1@example.com"
    assert cfg.rounds_before_rotation == 3
    assert cfg.round_delay_min == 180.0
    assert cfg.log_level == "INFO"
    assert cfg.state_file_path == "state.json"


def test_load_config_missing_required(monkeypatch):
    # Очищаем все требуемые переменные
    for k in [
        "BROWSERLESS_WS", "OPENAI_API_KEY", "TG_BOT_TOKEN", "TG_CHAT_IDS",
        "MAIN_EMAIL", "MAIN_PASSWORD", "DONOR_ACCOUNTS", "USER_DATA_JSON"
    ]:
        monkeypatch.delenv(k, raising=False)

    with pytest.raises(ConfigError) as exc_info:
        load_config(env_file=None)

    err_msg = str(exc_info.value)
    assert "Отсутствуют обязательные переменные" in err_msg
    assert "BROWSERLESS_WS" in err_msg
    assert "MAIN_EMAIL" in err_msg


def test_load_config_invalid_json(monkeypatch):
    env = get_valid_env()
    env["DONOR_ACCOUNTS"] = "NOT_JSON"
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    with pytest.raises(ConfigError) as exc_info:
        load_config(env_file=None)

    assert "DONOR_ACCOUNTS" in str(exc_info.value)


def test_mask_secret():
    assert mask_secret("sk-123456789", 4) == "sk-1...***"
    assert mask_secret("ab", 4) == "***"
    assert mask_secret("", 4) == "<not set>"
    assert mask_secret(None, 4) == "<not set>"


def test_load_config_check_interval_minutes(monkeypatch):
    env = get_valid_env()
    env["CHECK_INTERVAL_MINUTES"] = "10"
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    cfg = load_config(env_file=None)
    assert cfg.check_interval_minutes == 10.0
    # 10 мин = 600с, разброс +/- 60с
    assert cfg.round_delay_min == 540.0
    assert cfg.round_delay_max == 660.0
    summary = cfg.safe_summary()
    assert summary["check_interval"] == "10.0 мин"

