#!/usr/bin/env python3
"""
Диагностический скрипт для проверки подключения к сервису Browserless и доступности VFS Global.
Не выполняет мониторинг и не требует ключей Telegram/OpenAI.

Использование:
    python check_connection.py
    python check_connection.py --ws "ws://browserless:3000/chromium?token=..."
"""

import argparse
import asyncio
import os
import sys
import time

from playwright.async_api import async_playwright

from browser_client import connect_to_browserless, setup_page
from config import URL_LOGIN, load_env_file


async def check_browserless_connection(ws_url: str) -> bool:
    print(f"[{time.strftime('%H:%M:%S')}] 🔍 Проверка подключения к Browserless: {ws_url.split('?')[0]}")
    t0 = time.time()

    async with async_playwright() as p:
        try:
            browser = await connect_to_browserless(p, ws_url)
            conn_time = time.time() - t0
            print(f"[{time.strftime('%H:%M:%S')}] ✅ Успешное подключение к Browserless ({conn_time:.2f}с)")

            context, page = await setup_page(browser)

            print(f"[{time.strftime('%H:%M:%S')}] 🌐 Переход на страницу VFS: {URL_LOGIN} ...")
            t_nav = time.time()
            response = await page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=45000)
            nav_time = time.time() - t_nav

            status_code = response.status if response else "unknown"
            title = await page.title()
            current_url = page.url

            print(f"[{time.strftime('%H:%M:%S')}] 📄 Ответ VFS: HTTP {status_code} ({nav_time:.2f}с)")
            print(f"[{time.strftime('%H:%M:%S')}] 📌 Заголовок страницы: '{title}'")
            print(f"[{time.strftime('%H:%M:%S')}] 🔗 Текущий URL: {current_url}")

            # Сохранение диагностического скриншота
            screenshot_path = "check_connection.png"
            await page.screenshot(path=screenshot_path)
            file_size_kb = os.path.getsize(screenshot_path) / 1024
            print(f"[{time.strftime('%H:%M:%S')}] 📸 Диагностический скриншот сохранен: {screenshot_path} ({file_size_kb:.1f} КБ)")

            await browser.close()
            print(f"[{time.strftime('%H:%M:%S')}] 🎉 Диагностика успешно завершена!")
            return True

        except Exception as exc:
            print(f"\n[{time.strftime('%H:%M:%S')}] ❌ ОШИБКА ДИАГНОСТИКИ: {exc}\n", file=sys.stderr)
            return False


def main():
    parser = argparse.ArgumentParser(description="Диагностика подключения к Browserless и VFS Global.")
    parser.add_argument(
        "--ws",
        type=str,
        default="",
        help="WebSocket URL Browserless (по умолчанию берется BROWSERLESS_WS из .env)"
    )
    parser.add_argument(
        "--host",
        type=str,
        default="",
        help="Хост сервера Browserless (например, localhost или browserless)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3000,
        help="Порт сервера Browserless (по умолчанию 3000)"
    )
    parser.add_argument(
        "--token",
        type=str,
        default="",
        help="Токен авторизации Browserless"
    )
    args = parser.parse_args()

    load_env_file(".env")

    ws_url = args.ws or os.getenv("BROWSERLESS_WS", "").strip()
    if not ws_url:
        host = args.host or os.getenv("BROWSERLESS_HOST", "").strip()
        port = args.port or int(os.getenv("BROWSERLESS_PORT", "3000"))
        token = args.token or os.getenv("BROWSERLESS_TOKEN", "").strip()
        is_secure = os.getenv("BROWSERLESS_SECURE", "false").lower() in ("true", "1", "yes")

        if host:
            proto = "wss" if is_secure else "ws"
            token_query = f"?token={token}&timeout=86400000" if token else "?timeout=86400000"
            ws_url = f"{proto}://{host}:{port}/chromium{token_query}"

    if not ws_url:
        print(
            "❌ Ошибка: Не указан адрес Browserless!\n"
            "Задайте BROWSERLESS_HOST или BROWSERLESS_WS в .env, либо передайте аргументы:\n"
            "    python check_connection.py --host localhost --port 3000 --token mytoken\n"
            "    python check_connection.py --ws 'ws://localhost:3000/chromium?token=...'",
            file=sys.stderr
        )
        sys.exit(1)

    success = asyncio.run(check_browserless_connection(ws_url))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
