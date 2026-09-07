# ==============================================================================
# Multi-Stage Dockerfile for Rust USP Core Worker (TR-369)
# ==============================================================================
# Architecture: Alpine Linux + musl for minimal footprint (<30 MB RAM)
# Builder: Alpine with musl-dev, protobuf, and protobuf-dev
# Runner: Minimal Alpine 3.19 with ca-certificates
# Strict Physical Limit: 500 MB (docker-compose.yml)
# ==============================================================================

# Stage 1: Builder
FROM rust:alpine AS builder

RUN apk add --no-cache \
    musl-dev \
    protobuf \
    protobuf-dev \
    build-base

ENV PROTOC=/usr/bin/protoc
ENV PROTOC_INCLUDE=/usr/include

WORKDIR /usr/src/app

# Pre-cache dependencies
COPY Cargo.toml ./
COPY build.rs ./
COPY proto ./proto

RUN mkdir src && \
    echo "fn main() {}" > src/main.rs && \
    cargo build --release && \
    rm -rf src

# Copy real source code
COPY src ./src

# Touch files to force rebuild of the real application binary
RUN touch src/main.rs build.rs && \
    cargo build --release

# Stage 2: Runtime
FROM alpine:3.19 AS runner

RUN apk add --no-cache ca-certificates tzdata

WORKDIR /app

COPY --from=builder /usr/src/app/target/release/rust-core /app/rust-core

ENV HEALTHCHECK_FILE=/tmp/healthy

CMD ["/app/rust-core"]
