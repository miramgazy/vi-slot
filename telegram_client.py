import asyncio
import base64
import logging
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import httpx
from openai import AsyncOpenAI

from config import Config
from state import BotState

logger = logging.getLogger("vfs_bot.telegram")


class TelegramClient:
    """Асинхронный клиент для взаимодействия с Telegram Bot API и OpenAI Vision."""

    def __init__(self, config: Config, state: BotState):
        self.config = config
        self.state = state
        self.bot_token = config.tg_bot_token
        self.chat_ids = config.tg_chat_ids
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"
        self.http_client: Optional[httpx.AsyncClient] = None
        self.openai_client = AsyncOpenAI(
            api_key=config.openai_api_key,
            timeout=30.0,
            max_retries=2
        )

    async def get_http_client(self) -> httpx.AsyncClient:
        """Ленивая инициализация httpx.AsyncClient."""
        if self.http_client is None or self.http_client.is_closed:
            self.http_client = httpx.AsyncClient(timeout=15.0)
        return self.http_client

    async def close(self) -> None:
        """Корректно закрывает HTTP-сессии."""
        if self.http_client and not self.http_client.is_closed:
            await self.http_client.aclose()
            self.http_client = None
        await self.openai_client.close()

    async def _post_with_retry(self, endpoint: str, data: dict = None, files: dict = None, max_retries: int = 3) -> Optional[dict]:
        """Отправка POST-запроса в Telegram API с экспоненциальным backoff."""
        client = await self.get_http_client()
        url = f"{self.api_base}/{endpoint}"

        for attempt in range(1, max_retries + 1):
            try:
                if files:
                    resp = await client.post(url, data=data, files=files, timeout=20.0)
                else:
                    resp = await client.post(url, json=data, timeout=15.0)

                resp.raise_for_status()
                res_json = resp.json()
                if res_json.get("ok"):
                    return res_json
                else:
                    logger.warning("[TG API ERR] Ответ Telegram: %s", res_json.get("description"))
                    return None
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                backoff = attempt * 2
                logger.warning(
                    "[TG API] Ошибка запроса (%s, попытка %d/%d): %s. Пауза %ds...",
                    endpoint, attempt, max_retries, exc, backoff
                )
                if attempt == max_retries:
                    logger.error("[TG API] Все %d попыток исчерпаны для %s", max_retries, endpoint)
                    return None
                await asyncio.sleep(backoff)
            except Exception as e:
                logger.error("[TG API] Непредвиденная ошибка при вызове %s: %s", endpoint, e)
                return None
        return None

    async def send_to_chat(self, chat_id: str, text: str, photo_path: Optional[str] = None) -> bool:
        """Отправляет текстовое сообщение или фото конкретному чату."""
        try:
            if photo_path and os.path.isfile(photo_path):
                # Читаем файл изображения для multipart-отправки
                with open(photo_path, "rb") as f:
                    file_bytes = f.read()
                files = {"photo": ("screenshot.png", file_bytes, "image/png")}
                data = {"chat_id": str(chat_id), "caption": text[:1024]}
                res = await self._post_with_retry("sendPhoto", data=data, files=files)
                return res is not None
            else:
                data = {"chat_id": str(chat_id), "text": text}
                res = await self._post_with_retry("sendMessage", data=data)
                return res is not None
        except Exception as e:
            logger.error("[TG] Сбой отправки сообщения чату %s: %s", chat_id, e)
            return False

    async def broadcast(self, text: str, photo_path: Optional[str] = None) -> None:
        """Рассылает сообщение всем чатам из белого списка TG_CHAT_IDS."""
        tasks = [self.send_to_chat(cid, text, photo_path) for cid in self.chat_ids]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def ask_vision(self, image_path: str, user_question: str) -> str:
        """Анализирует скриншот через OpenAI GPT-4o-mini с таймаутом и обработкой ошибок."""
        if not os.path.isfile(image_path):
            return "Скриншот недоступен для анализа."

        try:
            with open(image_path, "rb") as f:
                base64_img = base64.b64encode(f.read()).decode("utf-8")

            active_email = (
                self.config.donor_accounts[self.state.current_donor_idx].email
                if self.config.donor_accounts else "<нет>"
            )

            applicant_name = (
                f"{self.config.user_data.get('first_name', '')} {self.config.user_data.get('last_name', '')}".strip()
            )

            system_prompt = f"""Ты — интеллектуальный персональный AI-ассистент по мониторингу визовых слотов VFS Global Italy в Казахстане (Астана, Алматы, Усть-Каменогорск).
Клиент: {applicant_name} (Студенческая виза: D Visa Study -> Enrollment at Universities).

КОНТЕКСТ:
- Непрерывная работа: {self.state.uptime_str()}
- Всего проверок слотов: {self.state.checks_count}
- Количество ротаций доноров: {self.state.rotations_count}
- Активный аккаунт (донор): {active_email}
- Найдено слотов: {len(self.state.slots_found_history)}
- Текущий статус: {self.state.current_status}

Ответь кратко, точно и прямо на вопрос пользователя: '{user_question}'."""

            response = await self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"Вопрос пользователя: {user_question}"},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_img}"}},
                        ],
                    },
                ],
                max_tokens=300,
                timeout=25.0,
            )
            return response.choices[0].message.content or "Пустой ответ от AI"
        except Exception as exc:
            logger.warning("[VISION] Ошибка запроса к OpenAI Vision: %s", exc)
            return f"Сбой Vision API: {exc}"

    async def run_listener(self, get_screenshot_cb: Callable[[], Any], stop_event: asyncio.Event) -> None:
        """
        Фоновый слушатель входящих команд Telegram с long-polling.
        get_screenshot_cb: асинхронная функция, возвращающая путь к свежему скриншоту страницы или None.
        """
        offset = 0
        client = await self.get_http_client()
        logger.info("[TG] Слушатель входящих сообщений запущен.")

        while not stop_event.is_set():
            try:
                url = f"{self.api_base}/getUpdates"
                params = {"offset": offset, "timeout": 10}
                resp = await client.get(url, params=params, timeout=20.0)

                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok") and data.get("result"):
                        for upd in data["result"]:
                            offset = upd["update_id"] + 1
                            msg = upd.get("message", {})
                            chat_id = str(msg.get("chat", {}).get("id", ""))
                            user_text = (msg.get("text") or "").strip()

                            # Проверка белого списка
                            if chat_id not in self.chat_ids or not user_text:
                                continue

                            logger.info("[TG] Сообщение от %s: '%s'", chat_id, user_text)
                            await self._handle_incoming_message(chat_id, user_text, get_screenshot_cb)
                else:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("[TG POLLING] Ошибка получения апдейтов: %s", e)
                await asyncio.sleep(3)

        logger.info("[TG] Слушатель входящих сообщений остановлен.")

    async def _handle_incoming_message(
        self, chat_id: str, user_text: str, get_screenshot_cb: Callable[[], Any]
    ) -> None:
        """Обработка команд и пользовательских запросов."""
        cmd = user_text.lower().split()[0]

        if cmd in ("/start", "/help"):
            help_text = (
                "🤖 *VFS Monitor Bot*\n\n"
                "Доступные команды:\n"
                "• `/status` — текущий статус, статистика и аптайм\n"
                "• `/screenshot` — получить текущий снимок экрана браузера\n"
                "• `/resume` — снять мониторинг с паузы\n"
                "• `/pause [минуты]` — поставить мониторинг на паузу\n"
                "• Любой текстовый вопрос — анализ текущей страницы через GPT-4o-mini"
            )
            await self.send_to_chat(chat_id, help_text)
            return

        if cmd == "/status":
            pause_info = ""
            if self.state.is_currently_paused():
                rem_mins = int(self.state.get_remaining_pause_seconds() / 60)
                pause_info = f"\n⏸ Пауза: осталось ~{rem_mins} мин."

            status_text = (
                f"📍 *Статус монитора:*\n"
                f"• Состояние: {self.state.current_status}{pause_info}\n"
                f"• Время работы: {self.state.uptime_str()}\n"
                f"• Проверок выполнено: {self.state.checks_count}\n"
                f"• Ротаций доноров: {self.state.rotations_count}\n"
                f"• Банов 429: {self.state.bans_429_count} | Ошибок 502: {self.state.errors_502_count}\n"
                f"• Найдено слотов: {len(self.state.slots_found_history)}\n"
                f"• Последняя проверка: {self.state.last_check_time}"
            )
            await self.send_to_chat(chat_id, status_text)
            return

        if cmd == "/resume":
            self.state.resume()
            await self.send_to_chat(chat_id, "▶️ Мониторинг возобновлен по команде пользователя.")
            return

        if cmd.startswith("/pause"):
            parts = user_text.split()
            mins = self.config.slot_pause_minutes
            if len(parts) > 1 and parts[1].isdigit():
                mins = int(parts[1])
            self.state.set_pause(mins, reason="Команда пользователя")
            await self.send_to_chat(chat_id, f"⏸ Мониторинг приостановлен на {mins} минут.")
            return

        # Запрос скриншота или текстовый вопрос через Vision AI
        shot_path = await get_screenshot_cb()
        if not shot_path:
            await self.send_to_chat(chat_id, f"📍 Статус: {self.state.current_status}\n(Браузер сейчас недоступен)")
            return

        if cmd == "/screenshot":
            await self.send_to_chat(chat_id, f"📸 Текущий вид страницы ({self.state.current_status})", photo_path=shot_path)
            return

        # Текстовый вопрос по содержимому экрана через GPT-4o-mini
        ai_reply = await self.ask_vision(shot_path, user_text)
        await self.send_to_chat(chat_id, f"🤖 {ai_reply}", photo_path=shot_path)
