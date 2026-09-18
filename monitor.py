import asyncio
import logging
import os
import random
import time
from typing import Optional

from playwright.async_api import Browser, Page

from browser_client import (
    click_appointment_button,
    dismiss_cookies,
    do_logout,
    is_ban_429,
    is_error_502,
    is_slot_available,
    robust_angular_fill,
    smart_select,
    sound_alert,
)
from config import Config, TARGET_CITIES, URL_LOGIN
from state import BotState
from telegram_client import TelegramClient

logger = logging.getLogger("vfs_bot.monitor")


async def safe_sleep(seconds: float, stop_event: asyncio.Event, step: float = 1.0) -> None:
    """Спит указанное количество секунд, периодически проверяя stop_event."""
    end_time = time.time() + seconds
    while time.time() < end_time and not stop_event.is_set():
        remaining = end_time - time.time()
        await asyncio.sleep(min(step, max(0.1, remaining)))


class VfsMonitor:
    """Оркестратор логики мониторинга слотов VFS Global."""

    def __init__(
        self,
        browser: Browser,
        config: Config,
        state: BotState,
        telegram_client: TelegramClient,
        stop_event: asyncio.Event,
    ):
        self.browser = browser
        self.config = config
        self.state = state
        self.telegram = telegram_client
        self.stop_event = stop_event

    async def get_active_page(self) -> Page:
        """Получает текущую активную страницу контекста или создает новую."""
        context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        pages = context.pages
        return pages[0] if pages else await context.new_page()

    async def handle_slot_found(self, page: Page, city_name: str) -> None:
        """Безопасная обработка обнаружения слота без закрытия браузера."""
        slot_shot = f"./slot_{city_name}.png"
        try:
            await page.screenshot(path=slot_shot)
        except Exception as e:
            logger.warning("Не удалось сделать скриншот слота: %s", e)
            slot_shot = None

        timestamp = self.state.record_slot(city_name)
        self.state.current_status = f"🔥 СЛОТ В {city_name.upper()}!"
        sound_alert()

        logger.info("=" * 60)
        logger.info("🔥 СЛОТ НАЙДЕН В %s (%s)!", city_name.upper(), timestamp)
        logger.info("=" * 60)

        # 1. Анализ через OpenAI Vision
        ai_reply = ""
        if slot_shot and os.path.isfile(slot_shot):
            ai_reply = await self.telegram.ask_vision(slot_shot, f"Подтверди найденный слот в городе {city_name}!")

        # 2. Оповещение в Telegram
        alert_msg = (
            f"🔥 *СЛОТ НАЙДЕН В {city_name.upper()}* ({timestamp})!\n\n"
            f"🔄 Переключаюсь на основной аккаунт `{self.config.main_email}`...\n"
        )
        if ai_reply:
            alert_msg += f"\n🧠 *Разбор AI:* {ai_reply}\n"

        await self.telegram.broadcast(alert_msg, photo_path=slot_shot)

        # 3. Перелогин в основной аккаунт
        logger.info("[ОСНОВНОЙ АККАУНТ] Вход под %s...", self.config.main_email)
        await do_logout(page)
        await page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        await robust_angular_fill(page, self.config.main_email, self.config.main_password)
        await page.wait_for_timeout(3000)

        # Переход к форме записи, если на дашборде
        if "dashboard" in page.url or "Активная(ые) запись(и)" in (await page.content()):
            await click_appointment_button(page)
            await page.wait_for_timeout(3000)

        # Скриншот основного аккаунта
        main_shot = "./main_account_view.png"
        try:
            await page.screenshot(path=main_shot)
        except Exception:
            main_shot = None

        # 4. Формирование ссылки Live View Browserless
        live_view_msg = (
            f"✅ *Основной аккаунт залогинен!*\n\n"
            f"Браузерная сессия удерживается активной. Вы можете перейти и завершить запись вручную.\n"
        )
        if self.config.browserless_debug_url:
            live_view_msg += f"\n🔗 [Перейти в Browserless Live View]({self.config.browserless_debug_url})\n"
        else:
            live_view_msg += (
                "\n💡 Для ручного завершения откройте сессию Browserless в веб-интерфейсе Coolify.\n"
            )

        live_view_msg += (
            f"\n⏸ Мониторинг приостановлен на {self.config.slot_pause_minutes} мин. "
            f"Для досрочного возобновления отправьте команду `/resume`."
        )

        await self.telegram.broadcast(live_view_msg, photo_path=main_shot)

        # 5. Установка паузы
        self.state.set_pause(self.config.slot_pause_minutes, reason=f"Слот найден в {city_name}")

    async def run(self) -> None:
        """Основной рабочий цикл мониторинга."""
        rounds_on_current_donor = 0
        logger.info("[МОНИТОР] Рабочий цикл запущен.")

        while not self.stop_event.is_set():
            try:
                # 0. Проверка режима паузы
                if self.state.is_currently_paused():
                    rem_sec = self.state.get_remaining_pause_seconds()
                    logger.debug("[МОНИТОР] Бот на паузе. Осталось %.1f сек.", rem_sec)
                    await safe_sleep(min(10.0, max(1.0, rem_sec)), self.stop_event)
                    continue

                page = await self.get_active_page()
                url = page.url
                content = await page.content()

                if not self.config.donor_accounts:
                    logger.error("[МОНИТОР] Список доноров пуст!")
                    await safe_sleep(10, self.stop_event)
                    continue

                active_donor = self.config.donor_accounts[self.state.current_donor_idx]

                # 1. Проверка бана 429
                if is_ban_429(content):
                    ban_shot = "./ban_429.png"
                    try:
                        await page.screenshot(path=ban_shot)
                    except Exception:
                        ban_shot = None

                    self.state.record_ban_429()
                    await self.telegram.broadcast(
                        f"⚠️ Донор {active_donor.email} получил бан 429 от VFS. Выполняю ротацию...",
                        photo_path=ban_shot
                    )
                    await do_logout(page)
                    new_idx = self.state.rotate_donor(len(self.config.donor_accounts), "Пойман бан 429")
                    next_donor = self.config.donor_accounts[new_idx]
                    self.state.current_status = f"Ротация (429) -> {next_donor.email}"
                    rounds_on_current_donor = 0
                    continue

                # 2. Проверка ошибки 502 / падения сервера
                if is_error_502(url, content):
                    err_shot = "./err_502.png"
                    try:
                        await page.screenshot(path=err_shot)
                    except Exception:
                        err_shot = None

                    self.state.record_error_502()
                    self.state.current_status = "Ошибка 502. Пауза 4 мин."
                    logger.warning("[ОШИБКА 502] Сервер VFS перегружен. Пауза 4 минуты...")
                    await self.telegram.broadcast("⚠️ Ошибка 502 на VFS. Сервер недоступен, пауза 4 мин.", photo_path=err_shot)
                    await safe_sleep(240, self.stop_event)
                    try:
                        await page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    continue

                # 3. На странице логина — заполняем донора
                if "login" in url:
                    self.state.current_status = f"Вход донора: {active_donor.email}"
                    await robust_angular_fill(page, active_donor.email, active_donor.password)
                    await page.wait_for_timeout(2500)
                    continue

                # 4. На дашборде — нажимаем кнопку записи
                if "dashboard" in url or "Активная(ые) запись(и)" in content:
                    if "application-detail" not in url:
                        await dismiss_cookies(page)
                        self.state.current_status = "На дашборде. Переход в форму..."
                        await click_appointment_button(page)
                        await page.wait_for_timeout(2500)
                        continue

                # 5. Ожидание формы заявки
                if "application-detail" not in url:
                    await page.wait_for_timeout(2000)
                    continue

                # 6. Обход городов в случайном порядке
                slot_found_flag = False
                shuffled_cities = random.sample(TARGET_CITIES, len(TARGET_CITIES))

                for city in shuffled_cities:
                    if self.stop_event.is_set():
                        break

                    self.state.current_status = f"Проверка: {city.name} ({active_donor.email})"
                    logger.info("[ГОРОД] Переключение на %s...", city.name)

                    c1 = await smart_select(page, 0, city.keywords, f"ВЦ {city.name}")
                    if not c1:
                        await page.wait_for_timeout(2000)
                        continue

                    c2 = await smart_select(
                        page, 1, ["D Visa Study", "Study", "Учеба", "Студент", "D Visa"], "Категория"
                    )
                    if not c2:
                        await page.wait_for_timeout(2000)
                        continue

                    c3 = await smart_select(
                        page, 2, ["Enrollment", "University", "Universities", "Университет", "ВУЗ"], "Подкатегория"
                    )
                    if not c3:
                        await page.wait_for_timeout(2000)
                        continue

                    # Проверка результата
                    await page.wait_for_timeout(3000)
                    btn_cont = page.locator("button:has-text('Продолжить'), button:has-text('Continue')").first

                    try:
                        is_active = await btn_cont.is_enabled()
                    except Exception:
                        is_active = False

                    try:
                        page_text = await page.locator("body").inner_text()
                    except Exception:
                        page_text = ""

                    self.state.record_check()

                    if is_slot_available(is_active, page_text):
                        slot_found_flag = True
                        await self.handle_slot_found(page, city.name)
                        break
                    else:
                        logger.info("[РЕЗУЛЬТАТ] %s: мест нет.", city.name)
                        city_delay = random.uniform(self.config.city_delay_min, self.config.city_delay_max)
                        logger.debug("[ПАУЗА] Ожидание %.1fс перед следующим городом...", city_delay)
                        await safe_sleep(city_delay, self.stop_event)

                if slot_found_flag:
                    rounds_on_current_donor = 0
                    continue

                rounds_on_current_donor += 1

                # Плановая ротация каждые N кругов
                if rounds_on_current_donor >= self.config.rounds_before_rotation:
                    logger.info(
                        "[РОТАЦИЯ] Донор отработал %d круга(ов). Выполняем плановую ротацию...",
                        self.config.rounds_before_rotation
                    )
                    await do_logout(page)
                    new_idx = self.state.rotate_donor(
                        len(self.config.donor_accounts),
                        f"Плановая ротация ({self.config.rounds_before_rotation} круга)"
                    )
                    rounds_on_current_donor = 0

                # Пауза между кругами
                round_delay = random.uniform(self.config.round_delay_min, self.config.round_delay_max)
                self.state.current_status = f"Круг #{rounds_on_current_donor} завершен. Пауза {round_delay / 60:.1f} мин."
                logger.info(
                    "[КРУГ ЗАВЕРШЕН] Пауза %.1f сек (%.1f мин) перед новым кругом...",
                    round_delay, round_delay / 60.0
                )
                await safe_sleep(round_delay, self.stop_event)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[МОНИТОР АВТОВОССТАНОВЛЕНИЕ] Ошибка (%s). Перезапуск шага...", exc)
                await safe_sleep(5, self.stop_event)

        logger.info("[МОНИТОР] Рабочий цикл остановлен.")
