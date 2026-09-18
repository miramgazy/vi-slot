"""
VFS Global Italy Slot Monitor (Казахстан)
Обновленная модульная версия без захардкоженных секретов.
Все настройки и секреты считываются из переменных окружения (.env).

Основная точка входа: bot.py
Данный файл сохранен для обратной совместимости.
"""

import asyncio
from bot import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass