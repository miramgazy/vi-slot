FROM python:3.12-slim

# Отключение буферизации вывода Python для мгновенного появления логов в Coolify
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Установка системных утилит (curl, ca-certificates) для надежной работы TLS
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Создание непривилегированного пользователя
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Установка зависимостей Python (без установки локальных браузеров playwright)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода приложения
COPY --chown=appuser:appuser . .

# Создание директории для персистентного хранения данных с правами appuser
RUN mkdir -p /app/data && chown -R appuser:appuser /app/data

# Переключение на непривилегированного пользователя
USER appuser

# Запуск бота в небуферизованном режиме
CMD ["python", "-u", "bot.py"]
