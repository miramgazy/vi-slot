import asyncio
import logging
import random
import time
from typing import List, Optional, Tuple

from playwright.async_api import Browser, BrowserContext, Page, Playwright

from config import Config, URL_LOGIN

logger = logging.getLogger("vfs_bot.browser")


# ==============================================================================
# ЧИСТЫЕ ФУНКЦИИ ДЕТЕКЦИИ (ДЛЯ ТЕСТОВ И РАНТАЙМА)
# ==============================================================================

def is_ban_429(page_content: str) -> bool:
    """Проверяет наличие признаков блокировки 429 по тексту страницы."""
    lowered = page_content.lower()
    return "429001" in lowered or "доступ ограничен для идентификатора" in lowered


def is_error_502(url: str, page_content: str) -> bool:
    """Проверяет наличие признаков ошибки 502 / падения сервера VFS."""
    if "page-not-found" in url.lower():
        return True
    lowered = page_content.lower()
    return "временная проблема с подключением" in lowered or "502 bad gateway" in lowered


def is_no_slots(page_text: str) -> bool:
    """Проверяет наличие явного сообщения об отсутствии свободных слотов."""
    lowered = " ".join(page_text.lower().split())
    return "нет доступных слотов" in lowered or "no appointment slots" in lowered


def is_slot_available(btn_enabled: bool, page_text: str) -> bool:
    """Определяет, найден ли активный слот."""
    if not btn_enabled:
        return False
    return not is_no_slots(page_text)


def sound_alert() -> None:
    """Безопасный звуковой сигнал (поддерживает Windows winsound и Linux bell)."""
    try:
        import winsound
        for _ in range(5):
            winsound.Beep(1000, 500)
    except Exception:
        # Для Linux / macOS / Docker выводим ASCII Bell символ
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass


# ==============================================================================
# ПОДКЛЮЧЕНИЕ К BROWSERLESS
# ==============================================================================

async def connect_to_browserless(playwright: Playwright, ws_url: str) -> Browser:
    """
    Подключается к Browserless с подробным логированием этапов и времени отклика.
    Сначала пробует connect_over_cdp (стандартный для Browserless Chromium),
    при ошибке протокола выполняет fallback на chromium.connect.
    """
    clean_endpoint = ws_url.split("?")[0]
    has_token = "token=" in ws_url
    logger.info(
        "[BROWSER] 🔌 Инициализация подключения к Browserless: %s (токен: %s, таймаут: 30с)...",
        clean_endpoint, "передан" if has_token else "не передан"
    )
    t_start = time.time()

    # Если URL прямо указывает на playwright endpoint (/playwright)
    if "/playwright" in ws_url:
        logger.info("[BROWSER] Обнаружен /playwright endpoint. Подключение через chromium.connect...")
        try:
            browser = await playwright.chromium.connect(ws_url, timeout=30000)
            elapsed = time.time() - t_start
            logger.info(
                "[BROWSER] ✅ Успешно подключено к Browserless (chromium.connect) за %.2fс! Chromium v%s",
                elapsed, browser.version
            )
            return browser
        except Exception as e:
            logger.warning("[BROWSER] ⚠️ Ошибка подключения через chromium.connect: %s. Пробуем CDP...", e)

    try:
        logger.info("[BROWSER] 🌐 Отправка CDP WebSocket рукопожатия к %s ...", clean_endpoint)
        browser = await playwright.chromium.connect_over_cdp(ws_url, timeout=30000)
        elapsed = time.time() - t_start
        logger.info(
            "[BROWSER] ✅ Успешное подключение к Browserless через CDP за %.2fс! Версия Chromium: %s (активных контекстов: %d)",
            elapsed, browser.version, len(browser.contexts)
        )
        return browser
    except Exception as cdp_err:
        elapsed_cdp = time.time() - t_start
        logger.warning(
            "[BROWSER] ⚠️ Не удалось подключиться через CDP за %.2fс: %s. Выполняем fallback на chromium.connect...",
            elapsed_cdp, cdp_err
        )
        try:
            t_conn = time.time()
            browser = await playwright.chromium.connect(ws_url, timeout=30000)
            elapsed = time.time() - t_conn
            logger.info(
                "[BROWSER] ✅ Успешно подключено через chromium.connect за %.2fс! Версия Chromium: %s",
                elapsed, browser.version
            )
            return browser
        except Exception as conn_err:
            logger.error("=" * 60)
            logger.error("[BROWSER] ❌ КРИТИЧЕСКАЯ ОШИБКА ПОДКЛЮЧЕНИЯ К BROWSERLESS!")
            logger.error("[BROWSER] Адрес сервера: %s", clean_endpoint)
            logger.error("[BROWSER] Ошибка CDP: %s", cdp_err)
            logger.error("[BROWSER] Ошибка Playwright Connect: %s", conn_err)
            logger.error(
                "[BROWSER] 💡 Диагностика: убедитесь, что сервис Browserless запущен, "
                "порт доступен и переменная TOKEN совпадает в обоих сервисах."
            )
            logger.error("=" * 60)
            raise conn_err


async def setup_page(browser: Browser) -> Tuple[BrowserContext, Page]:
    """Создает или получает активную страницу и внедряет антидетект-скрипты."""
    if len(browser.contexts) > 0:
        context = browser.contexts[0]
    else:
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="ru-RU",
        )

    pages = context.pages
    page = pages[0] if pages else await context.new_page()

    # Скрытие признаков автоматизации
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.navigator.chrome = { runtime: {} };
    """)

    return context, page


# ==============================================================================
# ДЕЙСТВИЯ СО СТРАНИЦЕЙ VFS
# ==============================================================================

async def dismiss_cookies(page: Page) -> None:
    """Закрытие баннера cookies."""
    try:
        cookie_btn = page.locator(
            "#onetrust-accept-btn-handler, "
            "button:has-text('Accept All Cookies'), "
            "button:has-text('Accept Only Necessary'), "
            "button:has-text('Принять все')"
        ).first
        if await cookie_btn.is_visible(timeout=1000):
            await cookie_btn.click(force=True)
            await page.wait_for_timeout(500)
    except Exception:
        pass


async def solve_cloudflare_turnstile(page: Page) -> bool:
    """Обнаружение и попытка клика по Cloudflare Turnstile без сторонних сервисов."""
    try:
        frames = page.frames
        for frame in frames:
            if "challenges.cloudflare.com" in frame.url or "turnstile" in frame.url:
                checkbox = frame.locator("input[type='checkbox'], span.mark, .ctp-checkbox-label, body").first
                if await checkbox.is_visible(timeout=1500):
                    logger.info("[КАПЧА] Обнаружен Turnstile! Кликаем чекбокс Cloudflare...")
                    await checkbox.click(force=True)
                    await page.wait_for_timeout(2500)
                    return True
    except Exception:
        pass
    return False


async def robust_angular_fill(page: Page, email: str, password: str) -> None:
    """Надежное заполнение формы логина в Angular-приложении VFS."""
    try:
        await dismiss_cookies(page)
        email_field = page.locator("input[formcontrolname='username'], input[type='email'], input#email").first
        pwd_field = page.locator("input[formcontrolname='password'], input[type='password'], input#password").first

        if await email_field.is_visible(timeout=3000):
            curr_val = await email_field.input_value()
            if curr_val != email:
                logger.info("[ВХОД] Ввод email: %s", email)
                await email_field.click(force=True)
                await page.wait_for_timeout(200)
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await email_field.press_sequentially(email, delay=45)

                logger.info("[ВХОД] Ввод пароля...")
                await pwd_field.click(force=True)
                await page.wait_for_timeout(200)
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await pwd_field.press_sequentially(password, delay=45)
                await page.keyboard.press("Tab")
                await page.wait_for_timeout(500)

            await solve_cloudflare_turnstile(page)

            btn = page.locator(
                "button:has-text('Войти'), button:has-text('Sign In'), button.mat-raised-button"
            ).first
            if await btn.is_visible(timeout=2000):
                is_disabled = await btn.get_attribute("disabled")
                if is_disabled is None:
                    logger.info("[ВХОД] Кнопка входа активна! Нажимаем Войти...")
                    await btn.click(force=True)
                    await page.wait_for_timeout(4000)
                else:
                    await solve_cloudflare_turnstile(page)
    except Exception as e:
        logger.warning("[!] Ошибка при заполнении формы логина: %s", e)


async def click_appointment_button(page: Page) -> bool:
    """Клик по кнопке 'Записаться на прием' на дашборде."""
    try:
        logger.info("[ДАШБОРД] Кликаем 'Записаться на прием'...")
        btn = page.locator(
            "button:has-text('Записаться на прием'), "
            "a:has-text('Записаться на прием'), "
            ".btn-brand-orange, "
            "a[href*='application-detail']"
        ).first

        if await btn.is_visible(timeout=3000):
            await btn.click(force=True)
            await page.wait_for_timeout(3000)
            return True

        clicked = await page.evaluate("""() => {
            const elements = Array.from(document.querySelectorAll('button, a, span'));
            const target = elements.find(el =>
                (el.textContent && el.textContent.includes('Записаться на прием')) ||
                el.classList.contains('btn-brand-orange')
            );
            if (target) {
                target.click();
                return true;
            }
            return false;
        }""")
        if clicked:
            await page.wait_for_timeout(3000)
            return True
    except Exception as e:
        logger.warning("[!] Ошибка клика по записи: %s", e)
    return False


async def smart_select(page: Page, idx: int, keywords: List[str], name: str) -> bool:
    """Выбор опции из выпадающего списка mat-select по ключевым словам."""
    try:
        await dismiss_cookies(page)
        selects = page.locator("mat-select")
        count = await selects.count()
        if count <= idx:
            return False

        dropdown = selects.nth(idx)
        aria_disabled = await dropdown.get_attribute("aria-disabled")
        if aria_disabled == "true":
            return False

        cur_text = await dropdown.inner_text()
        for kw in keywords:
            if kw.lower() in cur_text.lower():
                return True

        await dropdown.click(force=True)
        await page.wait_for_timeout(random.randint(1200, 1800))

        options = page.locator("mat-option")
        opt_count = await options.count()
        if opt_count == 0:
            await page.keyboard.press("Escape")
            return False

        for i in range(opt_count):
            opt_text = (await options.nth(i).inner_text()).strip()
            for kw in keywords:
                if kw.lower() in opt_text.lower():
                    logger.info("[ВЫБОР] В списке '%s' выбран пункт: '%s'", name, opt_text)
                    await options.nth(i).click(force=True)
                    await page.wait_for_timeout(random.randint(2500, 3500))
                    return True

        await page.keyboard.press("Escape")
        return False
    except Exception as e:
        logger.debug("[smart_select] Ошибка выбора в '%s': %s", name, e)
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return False


async def do_logout(page: Page) -> None:
    """Выход из текущего аккаунта VFS и очистка cookie."""
    try:
        logout = page.locator(
            "button:has-text('Выйти'), a:has-text('Выйти'), button:has-text('Sign out')"
        ).first
        if await logout.is_visible(timeout=2000):
            await logout.click(force=True)
            await page.wait_for_timeout(2000)
    except Exception:
        pass

    try:
        await page.context.clear_cookies()
        await page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
    except Exception as e:
        logger.warning("[LOGOUT] Ошибка при переходе на страницу логина: %s", e)
