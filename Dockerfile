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

USER appuser

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["/healthcheck.sh"]

ENTRYPOINT ["/entrypoint.sh"]
CMD []
