import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class ConfigError(ValueError):
    """Исключение при ошибке валидации конфигурации."""
    pass


def load_env_file(filepath: str = ".env") -> None:
    """Загружает переменные из .env файла в os.environ (без перезаписи существующих)."""
    if not os.path.isfile(filepath):
        return

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                # Удаляем парные внешние кавычки при наличии
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception as e:
        logging.warning("Не удалось прочитать .env файл: %s", e)


def mask_secret(secret: Optional[str], visible_chars: int = 4) -> str:
    """Маскирует секретную строку для безопасного логирования."""
    if not secret:
        return "<not set>"
    if len(secret) <= visible_chars:
        return "***"
    return f"{secret[:visible_chars]}...***"


@dataclass
class TargetCity:
    name: str
    keywords: List[str]


TARGET_CITIES: List[TargetCity] = [
    TargetCity(name="Астана", keywords=["Astana", "Астана"]),
    TargetCity(name="Алматы", keywords=["Almaty", "Алматы"]),
    TargetCity(name="Усть-Каменогорск", keywords=["Ust", "Каменогорск", "Oskemen", "Оскемен"]),
]

URL_LOGIN = "https://visa.vfsglobal.com/kaz/ru/ita/login"


@dataclass
class DonorAccount:
    email: str
    password: str


@dataclass
class Config:
    # Обязательные параметры
    browserless_ws: str
    openai_api_key: str
    tg_bot_token: str
    tg_chat_ids: List[str]
    main_email: str
    main_password: str
    donor_accounts: List[DonorAccount]
    user_data: Dict[str, Any]

    # Опциональные параметры с дефолтами
    rounds_before_rotation: int = 3
    round_delay_min: float = 180.0
    round_delay_max: float = 300.0
    city_delay_min: float = 15.0
    city_delay_max: float = 25.0
    log_level: str = "INFO"
    slot_pause_minutes: int = 60
    browserless_debug_url: str = ""
    state_file_path: str = "state.json"
    check_interval_minutes: Optional[float] = None

    def safe_summary(self) -> Dict[str, Any]:
        """Возвращает безопасное представление конфигурации без секретов."""
        interval_desc = (
            f"{self.check_interval_minutes} мин"
            if self.check_interval_minutes is not None
            else f"{self.round_delay_min / 60:.1f}–{self.round_delay_max / 60:.1f} мин"
        )
        return {
            "browserless_ws": mask_secret(self.browserless_ws, 12),
            "openai_api_key": mask_secret(self.openai_api_key, 7),
            "tg_bot_token": mask_secret(self.tg_bot_token, 8),
            "tg_chat_ids": self.tg_chat_ids,
            "main_email": self.main_email,
            "main_password": mask_secret(self.main_password, 2),
            "donor_count": len(self.donor_accounts),
            "donor_emails": [d.email for d in self.donor_accounts],
            "applicant": f"{self.user_data.get('first_name', '')} {self.user_data.get('last_name', '')}".strip(),
            "rounds_before_rotation": self.rounds_before_rotation,
            "check_interval": interval_desc,
            "round_delay_range": f"{self.round_delay_min:.0f}–{self.round_delay_max:.0f}s",
            "city_delay_range": f"{self.city_delay_min:.0f}–{self.city_delay_max:.0f}s",
            "log_level": self.log_level,
            "slot_pause_minutes": self.slot_pause_minutes,
            "browserless_debug_url": self.browserless_debug_url or "<not configured>",
            "state_file_path": self.state_file_path,
        }


def load_config(env_file: Optional[str] = ".env", exit_on_error: bool = False) -> Config:
    """
    Загружает и валидирует конфигурацию бота.
    Если exit_on_error=True и есть ошибки, логирует и делает sys.exit(1).
    """
    if env_file:
        load_env_file(env_file)

    missing: List[str] = []

    def get_req(key: str) -> str:
        val = os.getenv(key, "").strip()
        if not val:
            missing.append(key)
        return val

    browserless_ws = get_req("BROWSERLESS_WS")
    openai_api_key = get_req("OPENAI_API_KEY")
    tg_bot_token = get_req("TG_BOT_TOKEN")
    tg_chat_ids_raw = get_req("TG_CHAT_IDS")
    main_email = get_req("MAIN_EMAIL")
    main_password = get_req("MAIN_PASSWORD")
    donor_accounts_raw = get_req("DONOR_ACCOUNTS")
    user_data_raw = get_req("USER_DATA_JSON")

    if missing:
        msg = f"Ошибка конфигурации! Отсутствуют обязательные переменные окружения: {', '.join(missing)}"
        if exit_on_error:
            sys.stderr.write(f"\n[FATAL CONFIG ERROR] {msg}\n\n")
            sys.exit(1)
        raise ConfigError(msg)

    # Парсинг TG_CHAT_IDS
    tg_chat_ids = [cid.strip() for cid in tg_chat_ids_raw.split(",") if cid.strip()]
    if not tg_chat_ids:
        msg = "Переменная TG_CHAT_IDS не содержит корректных идентификаторов чатов."
        if exit_on_error:
            sys.stderr.write(f"\n[FATAL CONFIG ERROR] {msg}\n\n")
            sys.exit(1)
        raise ConfigError(msg)

    # Парсинг DONOR_ACCOUNTS
    try:
        donor_data = json.loads(donor_accounts_raw)
        if not isinstance(donor_data, list) or len(donor_data) == 0:
            raise ValueError("DONOR_ACCOUNTS должен быть непустым JSON-массивом")
        donor_accounts = [
            DonorAccount(email=d["email"].strip(), password=d["password"])
            for d in donor_data
        ]
    except Exception as e:
        msg = f"Ошибка парсинга DONOR_ACCOUNTS (ожидается JSON вида [{{'email':'..','password':'..'}}]): {e}"
        if exit_on_error:
            sys.stderr.write(f"\n[FATAL CONFIG ERROR] {msg}\n\n")
            sys.exit(1)
        raise ConfigError(msg)

    # Парсинг USER_DATA_JSON
    try:
        user_data = json.loads(user_data_raw)
        if not isinstance(user_data, dict):
            raise ValueError("USER_DATA_JSON должен быть JSON-объектом")
    except Exception as e:
        msg = f"Ошибка парсинга USER_DATA_JSON (ожидается JSON-объект с данными заявителя): {e}"
        if exit_on_error:
            sys.stderr.write(f"\n[FATAL CONFIG ERROR] {msg}\n\n")
            sys.exit(1)
        raise ConfigError(msg)

    # Опциональные параметры с безопасным приведением типов
    try:
        rounds_before_rotation = int(os.getenv("ROUNDS_BEFORE_ROTATION", "3"))
    except ValueError:
        rounds_before_rotation = 3

    check_interval_minutes: Optional[float] = None
    check_interval_raw = os.getenv("CHECK_INTERVAL_MINUTES", "").strip()
    if check_interval_raw:
        try:
            check_interval_minutes = float(check_interval_raw)
            base_sec = check_interval_minutes * 60.0
            jitter = max(15.0, base_sec * 0.1)
            round_delay_min = max(10.0, base_sec - jitter)
            round_delay_max = base_sec + jitter
        except ValueError:
            check_interval_minutes = None
            round_delay_min = 180.0
            round_delay_max = 300.0
    else:
        try:
            round_delay_min = float(os.getenv("ROUND_DELAY_MIN", "180"))
        except ValueError:
            round_delay_min = 180.0

        try:
            round_delay_max = float(os.getenv("ROUND_DELAY_MAX", "300"))
        except ValueError:
            round_delay_max = 300.0

    try:
        city_delay_min = float(os.getenv("CITY_DELAY_MIN", "15"))
    except ValueError:
        city_delay_min = 15.0

    try:
        city_delay_max = float(os.getenv("CITY_DELAY_MAX", "25"))
    except ValueError:
        city_delay_max = 25.0

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        log_level = "INFO"

    try:
        slot_pause_minutes = int(os.getenv("SLOT_PAUSE_MINUTES", "60"))
    except ValueError:
        slot_pause_minutes = 60

    browserless_debug_url = os.getenv("BROWSERLESS_DEBUG_URL", "").strip()
    state_file_path = os.getenv("STATE_FILE_PATH", "state.json").strip()

    return Config(
        browserless_ws=browserless_ws,
        openai_api_key=openai_api_key,
        tg_bot_token=tg_bot_token,
        tg_chat_ids=tg_chat_ids,
        main_email=main_email,
        main_password=main_password,
        donor_accounts=donor_accounts,
        user_data=user_data,
        rounds_before_rotation=rounds_before_rotation,
        round_delay_min=round_delay_min,
        round_delay_max=round_delay_max,
        city_delay_min=city_delay_min,
        city_delay_max=city_delay_max,
        log_level=log_level,
        slot_pause_minutes=slot_pause_minutes,
        browserless_debug_url=browserless_debug_url,
        state_file_path=state_file_path,
        check_interval_minutes=check_interval_minutes,
    )
