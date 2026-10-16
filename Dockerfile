FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/healthcheck.sh /healthcheck.sh
RUN chmod +x /entrypoint.sh /healthcheck.sh \
    && groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --no-create-home --shell /usr/sbin/nologin appuser

# Declared after the install so changing the commit does not rebuild the
# dependency layer. `docker inspect` shows the label; /app/REVISION is the
# same value for `docker exec`.
ARG GIT_COMMIT=unknown
ARG APP_VERSION=0.10.1
LABEL org.opencontainers.image.title="netbox-unifi-client-sync" \
      org.opencontainers.image.description="Sync UniFi clients and switch-port cables into NetBox" \
      org.opencontainers.image.source="https://github.com/LordAdama/netbox-unifi-client-sync" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}"
RUN printf '%s\n' "${GIT_COMMIT}" > /app/REVISION \
    && printf '%s\n' "${APP_VERSION}" > /app/VERSION \
    && chmod a+r /app/REVISION /app/VERSION

USER appuser

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["/healthcheck.sh"]

ENTRYPOINT ["/entrypoint.sh"]
CMD []
