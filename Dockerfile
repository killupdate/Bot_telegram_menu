FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DB_PATH=/app/data/bot.sqlite3
WORKDIR /app
RUN groupadd --gid 10001 bot && useradd --uid 10001 --gid bot --no-create-home bot \
    && mkdir /app/data && chown bot:bot /app/data
COPY --chown=bot:bot bot ./bot
USER bot
CMD ["python", "-m", "bot.app"]
