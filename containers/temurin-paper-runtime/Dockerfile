FROM docker.io/itzg/minecraft-server:2026.7.2-java25@sha256:6ec1110e4d9236d00ae9436a3e4a5929583e5b19cc94b756a7c603f7cf647a77

RUN test "$JAVA_HOME" = "/opt/java/openjdk" \
    && rm -rf /opt/java/openjdk \
    && mkdir -p /opt/java/openjdk

COPY runtime/ /opt/java/openjdk/

ENV JAVA_VERSION=jdk-25.0.5+8

LABEL org.opencontainers.image.source="https://github.com/Naohiro2g/mc-remote-stack"
LABEL org.opencontainers.image.version="temurin-25.0.5-8"
