# RelayOps has no third-party runtime dependency, so the "build" stage exists to
# assemble and byte-compile the application, not to install packages. The runtime
# stage copies only what the process needs and runs it as a non-root user.
FROM python:3.12.7-slim AS build

WORKDIR /build
COPY app ./app
COPY server.py requirements.txt ./
COPY static ./static
COPY docs/openapi.v1.yaml ./docs/openapi.v1.yaml
RUN python -m compileall -q app server.py


FROM python:3.12.7-slim AS runtime

LABEL org.opencontainers.image.title="RelayOps" \
      org.opencontainers.image.description="Retail operations control plane: REST API, signed webhooks, n8n integration, standard-library runtime." \
      org.opencontainers.image.source="https://github.com/Mohamed3042/ai-automation-command-center" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RELAYOPS_DB=/data/command_center.db \
    RELAYOPS_LOG_FORMAT=json

RUN useradd --system --create-home --uid 10001 relayops \
    && mkdir -p /data /app/data/reports \
    && chown -R relayops:relayops /data /app

WORKDIR /app
COPY --from=build --chown=relayops:relayops /build /app

USER relayops
VOLUME ["/data"]
EXPOSE 4173

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4173/api/v1/ready', timeout=4).status == 200 else 1)"

ENTRYPOINT ["python", "server.py"]
CMD ["--host", "0.0.0.0", "--port", "4173"]
