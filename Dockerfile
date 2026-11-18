FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONNOUSERSITE=1 \
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false
# Create the runtime user first so its home directory is writable
# (the Responses store and the Claude CLI both write under $HOME).
RUN useradd --create-home --uid 10001 pmo
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
USER 10001:10001
ENV HOME=/home/pmo
EXPOSE 8088
CMD ["pmo-agent"]
