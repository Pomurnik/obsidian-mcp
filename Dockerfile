FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

RUN uv sync --frozen --no-dev --no-editable

FROM build AS test
COPY tests/test_tools_attachment_mutations.py /tmp/attachment-tests/
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
RUN /app/.venv/bin/python -m unittest discover -s /tmp/attachment-tests -v \
    && touch /tmp/attachment-tests-passed

FROM python:3.12-slim AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# This dependency ensures the tests run before a runtime image can be built.
COPY --from=test /tmp/attachment-tests-passed /app/.attachment-tests-passed
ENV PATH=/app/.venv/bin:$PATH HOME=/tmp PYTHONDONTWRITEBYTECODE=1

# Only meaningful for network transports (http/sse) where the server binds a
# port; harmless no-op for stdio use. No curl in this base image, so use the
# stdlib instead of adding a dependency just for the healthcheck.
HEALTHCHECK --interval=30s --timeout=3s --start-period=60s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)" || exit 1

ENTRYPOINT ["obsidian-remote-mcp"]
