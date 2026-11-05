# The assistant example as a container. Build from the repository root:
#   docker build -f examples/assistant/Dockerfile -t lfg-assistant .
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "streamlit>=1.38" "httpx>=0.27"
COPY examples/assistant/app.py examples/assistant/finops_client.py /app/
# the Q&A panel's context: the repository's documentation
COPY README.md /app/docs-root/README.md
COPY docs /app/docs-root/docs
ENV LFG_ASSISTANT_DOCS_DIR=/app/docs-root
EXPOSE 8501
CMD ["streamlit", "run", "/app/app.py", "--server.port", "8501", "--server.address", "0.0.0.0", "--server.headless", "true"]
