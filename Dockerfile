FROM docker.io/library/python:3.12-slim

# Pinned to the versions the local test suite runs against (see pyproject.toml).
RUN pip install --no-cache-dir fastapi==0.115.5 "uvicorn[standard]==0.34.2" websockets==15.0.1 "pydantic>=2.9,<3"

ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
WORKDIR /app
COPY profiler /app/profiler

# One image runs the game or any bundled player; the manifest picks the command.
CMD ["python", "-m", "profiler.game.server"]
