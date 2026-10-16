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
# Opt-in: clone netbox-community/devicetype-library into the image.
# Leave false (the default) and mount a clone, or set DEVICETYPE_LIBRARY_PATH.
ARG FETCH_DEVICETYPE_LIBRARY=false
RUN if [ "$FETCH_DEVICETYPE_LIBRARY" = "true" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends git ca-certificates \
      && rm -rf /var/lib/apt/lists/* \
      && git clone --depth 1 https://github.com/netbox-community/devicetype-library /opt/devicetype-library \
      && rm -rf /opt/devicetype-library/.git \
      && chmod -R a+rX /opt/devicetype-library; \
    fi

ARG GIT_COMMIT=unknown
ARG APP_VERSION=0.12.0
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
