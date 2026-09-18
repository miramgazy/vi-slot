import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger("vfs_bot.state")


@dataclass
class BotState:
    start_time: datetime = field(default_factory=datetime.now)
    checks_count: int = 0
    rotations_count: int = 0
    bans_429_count: int = 0
    errors_502_count: int = 0
    slots_found_history: List[str] = field(default_factory=list)
    current_status: str = "Инициализация..."
    last_check_time: str = "Еще не проводилась"
    current_donor_idx: int = 0
    is_paused: bool = False
    pause_until_ts: Optional[float] = None
    state_file_path: Optional[str] = None

    def uptime_str(self) -> str:
        """Возвращает строку времени работы (ч. мин. сек)."""
        uptime_sec = int((datetime.now() - self.start_time).total_seconds())
        hours = uptime_sec // 3600
        mins = (uptime_sec % 3600) // 60
        secs = uptime_sec % 60
        return f"{hours} ч. {mins} мин. {secs} с."

    def rotate_donor(self, donor_count: int, reason: str = "") -> int:
        """Переключает индекс активного донора по кругу."""
        if donor_count <= 0:
            return 0
        self.rotations_count += 1
        self.current_donor_idx = (self.current_donor_idx + 1) % donor_count
        self.save()
        return self.current_donor_idx

    def record_check(self) -> None:
        """Фиксирует успешную проверку."""
        self.checks_count += 1
        self.last_check_time = time.strftime("%H:%M:%S")
        self.save()

    def record_ban_429(self) -> None:
        """Фиксирует блокировку 429."""
        self.bans_429_count += 1
        self.save()

    def record_error_502(self) -> None:
        """Фиксирует ошибку 502."""
        self.errors_502_count += 1
        self.save()

    def record_slot(self, city_name: str) -> str:
        """Фиксирует обнаруженный слот."""
        timestamp = time.strftime("%H:%M:%S")
        entry = f"Слот ({city_name}) в {timestamp}"
        self.slots_found_history.append(entry)
        self.save()
        return timestamp

    def set_pause(self, minutes: int, reason: str = "") -> None:
        """Устанавливает паузу мониторинга на N минут."""
        self.is_paused = True
        self.pause_until_ts = time.time() + (minutes * 60)
        self.current_status = f"Пауза на {minutes} мин ({reason})" if reason else f"Пауза на {minutes} мин"
        logger.info("[STATE] %s", self.current_status)
        self.save()

    def resume(self) -> None:
        """Снимает мониторинг с паузы."""
        self.is_paused = False
        self.pause_until_ts = None
        self.current_status = "Мониторинг возобновлен"
        logger.info("[STATE] Мониторинг снят с паузы")
        self.save()

    def is_currently_paused(self) -> bool:
        """Проверяет, действует ли сейчас пауза."""
        if not self.is_paused:
            return False
        if self.pause_until_ts is not None:
            if time.time() >= self.pause_until_ts:
                self.is_paused = False
                self.pause_until_ts = None
                self.save()
                return False
        return True

    def get_remaining_pause_seconds(self) -> float:
        """Возвращает остаток времени паузы в секундах."""
        if not self.is_currently_paused() or self.pause_until_ts is None:
            return 0.0
        return max(0.0, self.pause_until_ts - time.time())

    def save(self) -> None:
        """Сохраняет состояние в файл, если путь указан."""
        if not self.state_file_path:
            return
        try:
            data = {
                "current_donor_idx": self.current_donor_idx,
                "checks_count": self.checks_count,
                "rotations_count": self.rotations_count,
                "bans_429_count": self.bans_429_count,
                "errors_502_count": self.errors_502_count,
                "slots_found_history": self.slots_found_history,
                "is_paused": self.is_paused,
                "pause_until_ts": self.pause_until_ts,
                "last_check_time": self.last_check_time,
            }
            # Атомарная запись во временный файл
            tmp_path = f"{self.state_file_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.state_file_path)
        except Exception as e:
            logger.warning("[STATE] Не удалось сохранить файл состояния: %s", e)

    @classmethod
    def load(cls, file_path: str, donor_count: int = 1) -> "BotState":
        """Загружает сохраненное состояние из файла (если файл существует)."""
        state = cls(state_file_path=file_path)
        if not os.path.isfile(file_path):
            return state

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            state.checks_count = int(data.get("checks_count", 0))
            state.rotations_count = int(data.get("rotations_count", 0))
            state.bans_429_count = int(data.get("bans_429_count", 0))
            state.errors_502_count = int(data.get("errors_502_count", 0))
            state.slots_found_history = list(data.get("slots_found_history", []))
            state.last_check_time = str(data.get("last_check_time", "Еще не проводилась"))
            state.is_paused = bool(data.get("is_paused", False))
            state.pause_until_ts = data.get("pause_until_ts")

            # Проверяем корректность индекса донора с учетом текущего кол-ва
            saved_idx = int(data.get("current_donor_idx", 0))
            if donor_count > 0:
                state.current_donor_idx = saved_idx % donor_count
            else:
                state.current_donor_idx = 0

            logger.info(
                "[STATE] Загружено состояние: донор #%d, проверок: %d, ротаций: %d",
                state.current_donor_idx + 1, state.checks_count, state.rotations_count
            )
        except Exception as e:
            logger.warning("[STATE] Ошибка чтения состояния из %s: %s", file_path, e)

        return state
