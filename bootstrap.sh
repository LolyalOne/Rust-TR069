#!/bin/bash
mkdir -p .devcontainer
mkdir -p mosquitto
mkdir -p postgres
mkdir -p rust-core/src
mkdir -p rust-core/proto
mkdir -p python-api/app

cat << 'EOF' > rust-core/proto/usp.proto
syntax = "proto3";
package usp.record;
message Record {
    string version = 1;
    string to_id = 2;
    string from_id = 3;
}
EOF

touch .devcontainer/devcontainer.json
touch .gitignore
touch docker-compose.yml
touch mosquitto/mosquitto.conf
touch postgres/init.sql
touch rust-core/Cargo.toml
touch rust-core/build.rs
touch rust-core/src/main.rs
touch rust-core/Dockerfile
touch python-api/requirements.txt
touch python-api/app/__init__.py
touch python-api/app/main.py
touch python-api/Dockerfile
touch python-api/gunicorn_conf.py
chmod +x bootstrap.sh
