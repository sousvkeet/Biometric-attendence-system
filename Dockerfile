# BioAttend – web portal + device worker image.  See docs/DEPLOYMENT.md → "Docker".
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VAR_DIR=/data LOG_TO_FILE=false GUNICORN_BIND=0.0.0.0:8090

# PostgreSQL client (pg_dump/pg_restore for backups) and a checksum-verified rclone.
ARG RCLONE_VERSION=1.75.1
ARG TARGETARCH=amd64
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client tzdata ca-certificates curl unzip \
 && cd /tmp \
 && curl -fsSLO "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}.zip" \
 && curl -fsSL "https://downloads.rclone.org/v${RCLONE_VERSION}/SHA256SUMS" | grep "linux-${TARGETARCH}.zip" | sha256sum -c - \
 && unzip -j rclone-*.zip '*/rclone' -d /usr/local/bin && rm rclone-*.zip \
 && apt-get purge -y curl unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin bioattend
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=bioattend:bioattend . .
RUN SECRET_KEY=build-only-not-secret-0123456789abcdefghijklmnop python manage.py collectstatic --noinput -v 0 \
 && chown -R bioattend:bioattend /app/staticfiles \
 && mkdir -p /data && chown bioattend:bioattend /data && chmod 700 /data \
 && chmod +x deploy/docker/entrypoint.sh

USER bioattend
VOLUME ["/data"]
EXPOSE 8090
ENTRYPOINT ["deploy/docker/entrypoint.sh"]
CMD ["web"]
