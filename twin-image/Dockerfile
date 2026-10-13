# Generic twin base image (version 2). It knows nothing about any particular application:
# Phase 3 builds per-snapshot images FROM this base and adds the synthetic files/services.
#
# v2 adds: python3 + the service emulator (runs the fake HTTP / PostgreSQL / Redis services inside the twin),
# and the real psql / redis-cli clients so an attacker finds the tools an admin would have.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash coreutils procps findutils grep sed gawk less nano curl wget ca-certificates \
      iproute2 net-tools iputils-ping openssh-client tar gzip \
      python3 postgresql-client redis-tools \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
 # remove every setuid/setgid bit: nothing inside the twin can escalate through them
 && find / -xdev -type f \( -perm -4000 -o -perm -2000 \) -exec chmod a-s {} + \
 # attacker account (uid 1000) and a separate system account (uid 1001) that owns PID 1 and the emulators
 && groupadd -g 1000 user && useradd -m -u 1000 -g 1000 -s /bin/bash user \
 && groupadd -g 1001 hornet-sys && useradd -r -u 1001 -g 1001 -s /usr/sbin/nologin hornet-sys \
 && mkdir -p /home/user /usr/share/hornet && chown 1000:1000 /home/user

COPY emulator/hornet_emulator.py /usr/share/hornet/emulator.py
RUN chmod 0644 /usr/share/hornet/emulator.py

LABEL com.hornet.role="twin-base" com.hornet.twin.version="2"
USER 1001:1001
WORKDIR /home/user
CMD ["sleep", "infinity"]
