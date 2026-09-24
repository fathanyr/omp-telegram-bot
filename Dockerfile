# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Match the UID/GID of the host user that owns the mounted workspace.
# Override at build time:  docker build --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
ARG UID=1000
ARG GID=1000
ARG USERNAME=omp

# System dependencies required by the omp CLI and git workflows
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    bash \
    ca-certificates \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Create an unprivileged user matching the host UID/GID so that files created
# inside the container keep correct ownership on mounted volumes.
RUN groupadd -g "${GID}" "${USERNAME}" \
    && useradd -u "${UID}" -g "${GID}" -m -s /bin/bash "${USERNAME}"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Do not mark arbitrary mounted repositories as safe. Matching host UID/GID
# lets Git trust repositories owned by the container user; fix ownership or
# configure an explicit safe.directory for a known repository if necessary.
COPY --chown=${USERNAME}:${USERNAME} bot.py ./

USER ${USERNAME}

ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
