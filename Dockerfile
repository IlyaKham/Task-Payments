# Двухстадийная сборка: колёса ставятся в отдельный venv, в финальный
# образ переезжает только он. Компиляторы и кэш pip в рантайме не нужны.

FROM python:3.14-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /src

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Пакет app ставится в venv целиком (см. [tool.setuptools.packages.find]),
# поэтому в рантайме исходники отдельно копировать не нужно.
COPY pyproject.toml ./
COPY app ./app
RUN pip install .


FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Миграции — данные, а не пакет: их запускает alembic из рабочего каталога.
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker-entrypoint.sh ./

RUN chmod +x docker-entrypoint.sh \
    && useradd --system --uid 10001 payments \
    && chown -R payments:payments /app
USER payments

EXPOSE 8080

# Проверка готовности идёт тем же маршрутом, что и у автопроверки, —
# без curl, которого в slim-образе нет.
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=12 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
