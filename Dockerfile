# syntax=docker/dockerfile:1
FROM node:22-alpine AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim AS backend
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home demo \
    && mkdir -p /data/uploads && chown -R demo:demo /data
COPY backend/app ./app
USER demo
EXPOSE 8000
CMD ["uvicorn", "app.deployment:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--limit-concurrency", "16", "--timeout-keep-alive", "5", "--no-proxy-headers"]

FROM caddy:2.10-alpine AS gateway
COPY --from=frontend-build /build/dist /srv
COPY deploy/Caddyfile /etc/caddy/Caddyfile
