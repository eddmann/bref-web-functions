#!/usr/bin/env bash
# Builds the Rust gateway and the runtime image (Bref's PHP image plus the gateway).
# Extra arguments go to `docker build`, e.g. --build-arg RUST_TARGET=aarch64-unknown-linux-musl.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec docker build --tag "${BREF_WEB_IMAGE:-bref-web-functions:local}" "$@" "${project_dir}"
