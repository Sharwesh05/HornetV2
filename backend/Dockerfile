FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10002 hornet
WORKDIR /srv
COPY pyproject.toml ./
COPY backend ./backend
RUN pip install --no-cache-dir ".[postgres]"
USER hornet
WORKDIR /srv/backend
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
