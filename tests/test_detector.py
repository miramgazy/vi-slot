import pytest
from browser_client import is_ban_429, is_error_502, is_no_slots, is_slot_available


def test_is_ban_429():
    assert is_ban_429("Ошибка 429001: Превышен лимит запросов")
    assert is_ban_429("Доступ ограничен для идентификатора пользователя")
    assert is_ban_429("ДОСТУП ОГРАНИЧЕН ДЛЯ ИДЕНТИФИКАТОРА")
    assert not is_ban_429("Добро пожаловать в систему VFS Global")
    assert not is_ban_429("")


def test_is_error_502():
    assert is_error_502("https://visa.vfsglobal.com/kaz/ru/ita/page-not-found", "Not found")
    assert is_error_502("https://visa.vfsglobal.com/login", "Временная проблема с подключением к серверу")
    assert is_error_502("https://visa.vfsglobal.com/login", "502 Bad Gateway nginx")
    assert not is_error_502("https://visa.vfsglobal.com/dashboard", "Успешный вход")


def test_is_no_slots():
    assert is_no_slots("В настоящее время нет доступных слотов для записи.")
    assert is_no_slots("There are no appointment slots available for this category.")
    assert is_no_slots("Нет    доступных    слотов")
    assert not is_no_slots("Доступные даты: 25.10.2024")
    assert not is_no_slots("")


def test_is_slot_available():
    # Кнопка неактивна -> слота нет
    assert not is_slot_available(btn_enabled=False, page_text="Выберите удобное время")
    
    # Кнопка активна, но написано "нет доступных слотов" -> слота нет
    assert not is_slot_available(btn_enabled=True, page_text="Нет доступных слотов на выбранную дату")
    
    # Кнопка активна и сообщение об отсутствии слотов отсутствует -> СЛОТ ЕСТЬ!
    assert is_slot_available(btn_enabled=True, page_text="Доступные даты для записи: 15 октября 10:30")
