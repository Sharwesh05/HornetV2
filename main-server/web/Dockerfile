# "Main server": a small but realistic production web app that the discovery agent will observe.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 shop \
 && mkdir -p /var/www/shop /var/log/shop \
 && chown -R shop:shop /var/www/shop /var/log/shop

WORKDIR /var/www/shop
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/

LABEL com.hornet.demo="main-server" \
      org.opencontainers.image.title="shop-web" \
      org.opencontainers.image.description="Demo production web app (fake data only)"

USER shop
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=2)"
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "app.server:app"]
