# syntax=docker/dockerfile:1.7
FROM python:3.15-slim AS build
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir ".[azure,postgres,litellm-ui]"

FROM python:3.15-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    LFG_PRICING_PATH=/app/config/pricing.yaml \
    LFG_DATABASE_URL=sqlite:////data/gateway.db
RUN useradd --system --uid 10001 --create-home gateway \
    && mkdir -p /data /app/config && chown -R gateway:gateway /data /app
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/llm-finops-gateway /usr/local/bin/llm-finops-gateway
COPY --chown=gateway:gateway config /app/config
WORKDIR /app
USER gateway
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c \
  "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"
CMD ["llm-finops-gateway", "--host", "0.0.0.0", "--port", "8080"]
