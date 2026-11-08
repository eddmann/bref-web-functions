#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
proxy_ca="${BREF_WEB_CA_BUNDLE:-${CODEX_PROXY_CERT:-/etc/ssl/certs/ca-certificates.crt}}"
image="${BREF_WEB_IMAGE:-bref-web-functions:local}"
exec env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS \
  -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH \
  docker --host=unix:///var/run/docker.sock build \
  --secret "id=proxy_ca,src=${proxy_ca}" \
  --tag "${image}" "$@" "${project_dir}"
