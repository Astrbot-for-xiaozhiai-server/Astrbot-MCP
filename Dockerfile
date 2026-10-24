FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 mcp \
    && mkdir -p /app/data \
    && chown -R mcp:mcp /app

COPY config.example.yaml /app/config.example.yaml

USER mcp
EXPOSE 8765
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).status == 200 else 1)"

# If you mount a config at /app/config.yaml it is picked up automatically.
CMD ["astrbot-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
