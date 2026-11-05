# LiteLLM proxy image with the FinOps plugin installed (ADR-0019).
# Build from the repository root:  docker build -f examples/litellm/Dockerfile -t litellm-finops .
ARG LITELLM_IMAGE=ghcr.io/berriai/litellm@sha256:c560d317136880ea92d62334d4367ab10955b1af189dd3ea24f6551290b1d6c1
FROM ${LITELLM_IMAGE}
COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN /app/.venv/bin/python -m pip install --no-cache-dir /src \
    || (/app/.venv/bin/python -m ensurepip --upgrade \
        && /app/.venv/bin/python -m pip install --no-cache-dir /src)
