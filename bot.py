import asyncio
import logging
import os
import signal
import sys
from typing import Optional

from playwright.async_api import async_playwright

from browser_client import connect_to_browserless, setup_page
from config import Config, URL_LOGIN, load_config
from monitor import VfsMonitor
from state import BotState
from telegram_client import TelegramClient


class SensitiveDataFilter(logging.Filter):
    """Фильтр для предотвращения попадания чувствительных токенов и паролей в лог."""

    def __init__(self, secrets=None):
        super().__init__()
        self.secrets = [s for s in (secrets or []) if s and len(s) > 3]

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            for sec in self.secrets:
                if sec in record.msg:
                    record.msg = record.msg.replace(sec, "***")
        return True


def setup_logging(config: Config) -> logging.Logger:
    """Настройка структурированного логирования со временем и фильтрацией секретов."""
    log_format = "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    level = getattr(logging, config.log_level, logging.INFO)
    logging.basicConfig(level=level, format=log_format, datefmt=date_format)

    # Список секретов для скрытия
    secrets = [
        config.openai_api_key,
        config.tg_bot_token,
        config.main_password,
    ]
    for d in config.donor_accounts:
        secrets.append(d.password)

    root_logger = logging.getLogger()
    filt = SensitiveDataFilter(secrets)
    root_logger.addFilter(filt)

    return logging.getLogger("vfs_bot")


async def main() -> None:
    """Главная точка входа бота."""
    # 1. Загрузка и валидация конфигурации
    config = load_config(env_file=".env", exit_on_error=True)
    logger = setup_logging(config)

    logger.info("=" * 60)
    logger.info("Запуск VFS Global Italy Slot Monitor")
    logger.info("Конфигурация: %s", config.safe_summary())
    logger.info("=" * 60)

    # 2. Инициализация состояния
    state = BotState.load(config.state_file_path, len(config.donor_accounts))

    # 3. Инициализация Telegram-клиента
    telegram_client = TelegramClient(config, state)

    # 4. Обработка сигналов завершения (SIGINT, SIGTERM)
    global_stop_event = asyncio.Event()

    def signal_handler():
        logger.info("[SHUTDOWN] Получен сигнал завершения. Останавливаем бота...")
        global_stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except (NotImplementedError, RuntimeError):
            # На некоторых платформах (например Windows) add_signal_handler может не поддерживаться
            pass

    # Отправка стартового сообщения в Telegram
    await telegram_client.broadcast(
        "🚀 *VFS Monitor Bot запущен!*\n\n"
        f"• Режим: Browserless WebSocket\n"
        f"• Города: Астана, Алматы, Усть-Каменогорск\n"
        f"• Активный донор: #{state.current_donor_idx + 1} ({config.donor_accounts[state.current_donor_idx].email})\n"
        "• Отправьте `/status` для просмотра статистики или `/help` для списка команд."
    )

    # 5. Цикл переподключения к Browserless
    reconnect_delay = 5
    max_reconnect_delay = 60

    async with async_playwright() as p:
        while not global_stop_event.is_set():
            browser = None
            session_stop_event = asyncio.Event()
            tg_task: Optional[asyncio.Task] = None
            mon_task: Optional[asyncio.Task] = None

            try:
                state.current_status = "Подключение к Browserless..."
                browser = await connect_to_browserless(p, config.browserless_ws)
                reconnect_delay = 5  # сбрасываем backoff при успехе

                context, page = await setup_page(browser)

                # Проверка стартовой страницы
                if "vfsglobal.com" not in page.url:
                    logger.info("[СТАРТ] Открываем страницу входа VFS...")
                    try:
                        await page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(2000)
                    except Exception as e:
                        logger.warning("[СТАРТ] Предупреждение при переходе на VFS: %s", e)

                monitor = VfsMonitor(
                    browser=browser,
                    config=config,
                    state=state,
                    telegram_client=telegram_client,
                    stop_event=session_stop_event,
                )

                async def get_current_screenshot() -> Optional[str]:
                    try:
                        p_active = await monitor.get_active_page()
                        shot_path = "./current_view.png"
                        await p_active.screenshot(path=shot_path)
                        return shot_path
                    except Exception as err:
                        logger.debug("Не удалось получить скриншот для Telegram: %s", err)
                        return None

                # Запуск фоновых задач сессии
                tg_task = asyncio.create_task(
                    telegram_client.run_listener(get_current_screenshot, session_stop_event)
                )
                mon_task = asyncio.create_task(monitor.run())

                logger.info("[SUPERVISOR] Фоновые задачи запущены. Контроль сессии активен.")

                # Мониторинг жизнеспособности сессии
                while not global_stop_event.is_set():
                    if not browser.is_connected():
                        logger.warning("[SUPERVISOR] Сессия Browserless оборвана (browser.is_connected() == False)!")
                        break

                    if tg_task.done() and tg_task.exception():
                        logger.error("[SUPERVISOR] Задача Telegram завершилась с ошибкой: %s", tg_task.exception())
                        break

                    if mon_task.done() and mon_task.exception():
                        logger.error("[SUPERVISOR] Задача мониторинга завершилась с ошибкой: %s", mon_task.exception())
                        break

                    await asyncio.sleep(3)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[SUPERVISOR] Ошибка подключения/сессии Browserless: %s", e)
            finally:
                # Корректная остановка текущей сессии
                session_stop_event.set()

                tasks_to_cancel = [t for t in (tg_task, mon_task) if t and not t.done()]
                for t in tasks_to_cancel:
                    t.cancel()
                if tasks_to_cancel:
                    await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass

            if not global_stop_event.is_set():
                logger.info(
                    "[SUPERVISOR] Ожидание %d сек перед повторным подключением к Browserless...",
                    reconnect_delay
                )
                state.current_status = f"Переподключение через {reconnect_delay}с..."
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

    # 6. Финальное завершение
    logger.info("[SHUTDOWN] Сохранение финального состояния...")
    state.save()
    await telegram_client.close()
    logger.info("[SHUTDOWN] Бот полностью остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
