# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Match the UID/GID of the host user that owns the mounted workspace, and the
# host `docker` group so the unprivileged bot process can open the socket.
# Override at build time:
#   docker build --build-arg UID=$(id -u) --build-arg GID=$(id -g) \
#                --build-arg DOCKER_GID=$(stat -c %g /var/run/docker.sock) .
ARG UID=1000
ARG GID=1000
ARG DOCKER_GID=999
ARG USERNAME=omp

# System dependencies required by the omp CLI, git workflows, and host control.
#   docker-cli  - talks to the host daemon through the mounted socket
#   util-linux  - nsenter, which enters the host namespaces (PID 1)
#   sudo        - lets the bot user reach the host as root without a password
#   iproute2    - ss/ip for host network inspection
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    bash \
    ca-certificates \
    curl \
    procps \
    iproute2 \
    util-linux \
    sudo \
    docker-cli \
    && rm -rf /var/lib/apt/lists/*

# Create an unprivileged user matching the host UID/GID so that files created
# inside the container keep correct ownership on mounted volumes. The extra
# group carries the host docker GID; without it the 0660 socket is unreadable.
RUN groupadd -g "${DOCKER_GID}" hostdocker \
    && groupadd -g "${GID}" "${USERNAME}" \
    && useradd -u "${UID}" -g "${GID}" -G hostdocker -m -s /bin/bash "${USERNAME}" \
    && echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${USERNAME} \
    && chmod 0440 /etc/sudoers.d/${USERNAME}

# Host escape hatch. `privileged: true` plus `pid: host` in Compose makes the
# host PID namespace reachable, so nsenter can run any binary against the real
# root filesystem, systemd, network, and process tree.
RUN mkdir -p /usr/local/lib/hostbin \
    && printf '%s\n' \
        '#!/bin/sh' \
        '# Run an arbitrary command in the host namespaces (mount/UTS/IPC/net/PID).' \
        '# Falls back to sudo when invoked by the unprivileged bot user.' \
        'if [ "$(id -u)" -eq 0 ]; then' \
        '    exec /usr/bin/nsenter -t 1 -m -u -i -n -p -- "$@"' \
        'else' \
        '    exec /usr/bin/sudo /usr/bin/nsenter -t 1 -m -u -i -n -p -- "$@"' \
        'fi' > /usr/local/lib/hostbin/host-exec \
    && printf '%s\n' \
        '#!/bin/sh' \
        '# Shim installed under a host command name: executes host binary in host namespaces.' \
        'cmd="$(basename "$0")"' \
        'if [ "$(id -u)" -eq 0 ]; then' \
        '    exec /usr/bin/nsenter -t 1 -m -u -i -n -p -- "$cmd" "$@"' \
        'else' \
        '    exec /usr/bin/sudo /usr/bin/nsenter -t 1 -m -u -i -n -p -- "$cmd" "$@"' \
        'fi' > /usr/local/lib/hostbin/host-name-exec \
    && chmod +x /usr/local/lib/hostbin/host-exec /usr/local/lib/hostbin/host-name-exec \
    && ln -sf /usr/local/lib/hostbin/host-exec /usr/local/bin/host-exec \
    && ln -sf /usr/local/lib/hostbin/host-exec /usr/local/bin/host \
    && for cmd in systemctl journalctl apt-get apt service ufw ss; do \
           ln -sf /usr/local/lib/hostbin/host-name-exec "/usr/local/bin/$cmd"; \
       done

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
