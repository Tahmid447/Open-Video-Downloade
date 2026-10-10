FROM denoland/deno:bin-2.9.7@sha256:bc5aa4466e21b6d3021226a85ba2e1911f7c386254d97b9d797903ab74edace2 AS deno
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DOWNLOAD_DIR=/data/downloads HOME=/tmp
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 downloader \
    && useradd --uid 10001 --gid 10001 --no-create-home downloader \
    && mkdir -p /data/downloads /data/library /data/state /data/config && chown -R 10001:10001 /data
COPY --from=deno /deno /usr/local/bin/deno
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip check
COPY app.py common.py worker.py network_guard.py database.py extraction.py media.py security.py accounts.py manage.py vault.py recovery.py ./
COPY static/ static/
COPY templates/ templates/
COPY deploy/healthcheck.sh /app/healthcheck.sh
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["/bin/sh", "/app/healthcheck.sh"]
EXPOSE 8000
# All interfaces INSIDE the isolated container; the host publishes localhost only.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--log-level", "warning", "--timeout-graceful-shutdown", "10"]
