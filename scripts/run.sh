#!/usr/bin/env bash
set -euo pipefail
mode="${1:-rust}"
port="${2:-8080}"
case "${mode}" in rust|caddy) ;; *) printf 'mode must be rust or caddy\n' >&2; exit 2;; esac
case "${port}" in ''|*[!0-9]*) printf 'port must be numeric\n' >&2; exit 2;; esac
image="${BREF_WEB_IMAGE:-bref-web-functions:local}"
exec env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS \
  -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH \
  docker --host=unix:///var/run/docker.sock run --rm \
  --read-only --tmpfs /tmp:rw,nosuid,nodev,size=512m,mode=1777 \
  --cpus 2 --memory 2g --pids-limit 256 \
  --publish "127.0.0.1:${port}:8080" \
  --env "BREF_WEB_GATEWAY=${mode}" \
  --env "BREF_WEB_FPM_TIMEOUT_SECONDS=${BREF_WEB_FPM_TIMEOUT_SECONDS:-28}" \
  --env "BREF_WEB_REQUEST_TIMEOUT_MS=${BREF_WEB_REQUEST_TIMEOUT_MS:-30000}" \
  "${image}"
