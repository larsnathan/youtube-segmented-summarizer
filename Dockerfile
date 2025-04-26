FROM ghcr.io/astral-sh/uv:debian-slim

RUN apt-get update && \
    # ffmpeg for muxing video+audio
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get purge -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml pyproject.toml
COPY uv.lock uv.lock
RUN uv sync --locked

COPY . /app/

ENV PYTHONUNBUFFERED=1
# Default command is to run the gui, overwrite in docker-compose.yaml
CMD ["uv", "run", "gui.py"] 