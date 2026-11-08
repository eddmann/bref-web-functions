#!/usr/bin/env bash
# Shared configuration for Bref's original /opt distribution or the same
# distribution relocated inside a deployment archive.
set -euo pipefail
umask 077

fail() {
    printf 'bref_web_compat configuration_error=%s\n' "$1" >&2
    exit 78
}

positive_integer() {
    [[ "$2" =~ ^[1-9][0-9]{0,6}$ ]] || fail "$1_must_be_a_positive_integer"
}

absolute_path() {
    [[ "$2" == /* && "$2" != *$'\n'* && "$2" != *$'\r'* && "$2" != *'"'* ]] \
        || fail "$1_must_be_a_safe_absolute_path"
}

compat_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export LAMBDA_TASK_ROOT="${LAMBDA_TASK_ROOT:-/var/task}"
export BREF_WEB_RUNTIME_ROOT="${BREF_WEB_RUNTIME_ROOT:-/opt}"
export HOME=/tmp/bref-web
export PATH="$BREF_WEB_RUNTIME_ROOT/bin:${PATH}"
export LD_LIBRARY_PATH="$BREF_WEB_RUNTIME_ROOT/lib:$BREF_WEB_RUNTIME_ROOT/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PHPRC="${PHPRC:-$BREF_WEB_RUNTIME_ROOT/bref/etc/php}"
export BREF_RUNTIME=fpm
export BREF_WEB_GATEWAY="${BREF_WEB_GATEWAY:-rust}"
export BREF_WEB_MAX_IN_FLIGHT="${BREF_WEB_MAX_IN_FLIGHT:-4}"
export BREF_WEB_FPM_TIMEOUT_SECONDS="${BREF_WEB_FPM_TIMEOUT_SECONDS:-28}"
export BREF_WEB_FPM_MAX_REQUESTS="${BREF_WEB_FPM_MAX_REQUESTS:-100}"
export BREF_WEB_PHP_MEMORY_LIMIT="${BREF_WEB_PHP_MEMORY_LIMIT:-128M}"
export BREF_WEB_OPCACHE_MEMORY_MB="${BREF_WEB_OPCACHE_MEMORY_MB:-128}"
export BREF_WEB_MAX_BODY_BYTES="${BREF_WEB_MAX_BODY_BYTES:-6291456}"
export BREF_WEB_FPM_ADDR="${BREF_WEB_FPM_ADDR:-127.0.0.1:9000}"
export BREF_WEB_FPM_BIN="${BREF_WEB_FPM_BIN:-$BREF_WEB_RUNTIME_ROOT/bin/php-fpm}"
export BREF_WEB_FPM_CONFIG="${BREF_WEB_FPM_CONFIG:-/tmp/bref-web/php-fpm.conf}"
export BREF_WEB_NODE_BIN="${BREF_WEB_NODE_BIN:-/usr/local/bin/node}"
export BREF_WEB_LAUNCHER="${BREF_WEB_LAUNCHER:-$LAMBDA_TASK_ROOT/runtime/launcher/index.js}"

case "$BREF_WEB_GATEWAY" in
    rust|caddy) ;;
    *) fail gateway_must_be_rust_or_caddy ;;
esac

positive_integer max_in_flight "$BREF_WEB_MAX_IN_FLIGHT"
(( BREF_WEB_MAX_IN_FLIGHT <= 128 )) || fail max_in_flight_exceeds_128
positive_integer fpm_timeout "$BREF_WEB_FPM_TIMEOUT_SECONDS"
positive_integer max_requests "$BREF_WEB_FPM_MAX_REQUESTS"
positive_integer opcache_memory "$BREF_WEB_OPCACHE_MEMORY_MB"
[[ "$BREF_WEB_MAX_BODY_BYTES" =~ ^[1-9][0-9]{0,9}$ ]] \
    || fail max_body_bytes_must_be_a_positive_integer
(( BREF_WEB_MAX_BODY_BYTES <= 2147483647 )) || fail max_body_bytes_exceeds_2147483647
absolute_path task_root "$LAMBDA_TASK_ROOT"
absolute_path runtime_root "$BREF_WEB_RUNTIME_ROOT"
absolute_path php_configuration "$PHPRC"
runtime_php="$BREF_WEB_RUNTIME_ROOT/bin/php"
[[ -x "$runtime_php" ]] || fail php_binary_is_not_executable
[[ "$BREF_WEB_PHP_MEMORY_LIMIT" =~ ^[1-9][0-9]{0,9}[KMG]?$ ]] \
    || fail php_memory_limit_must_be_positive_bytes_or_uppercase_K_M_G
[[ "$BREF_WEB_FPM_ADDR" =~ ^127\.0\.0\.1:([1-9][0-9]{0,4})$ ]] \
    || fail fpm_addr_must_be_loopback_ipv4_and_port
(( BASH_REMATCH[1] <= 65535 )) || fail fpm_port_out_of_range
absolute_path fpm_config "$BREF_WEB_FPM_CONFIG"
[[ -x "$BREF_WEB_FPM_BIN" ]] || fail fpm_binary_is_not_executable

mkdir -p "$HOME/conf.d" "$HOME/tmp" "$HOME/sessions" \
    "$(dirname "$BREF_WEB_FPM_CONFIG")"
export TMPDIR="$HOME/tmp"

# Preserve application scan directories, always include the actual runtime's
# defaults, and exclude a previous generated override while reading user INI.
# Mapping the original Bref default enables an existing PHP_INI_SCAN_DIR to work
# after relocation, without scanning/loading the same extensions twice.
runtime_scan="$BREF_WEB_RUNTIME_ROOT/bref/etc/php/conf.d"
scan_path="$runtime_scan"
IFS=: read -r -a application_scan_paths <<< "${PHP_INI_SCAN_DIR:-$LAMBDA_TASK_ROOT/php/conf.d}"
for directory in "${application_scan_paths[@]}"; do
    [[ -n "$directory" ]] || continue
    [[ "$directory" != /opt/bref/etc/php/conf.d || "$BREF_WEB_RUNTIME_ROOT" == /opt ]] || directory="$runtime_scan"
    [[ "$directory" != "$HOME/conf.d" ]] || continue
    [[ ":$scan_path:" != *":$directory:"* ]] || continue
    scan_path+=":$directory"
done
export PHP_INI_SCAN_DIR="$scan_path"

if [[ -z "${SSL_CERT_FILE:-}" ]]; then
    for bundle in "$BREF_WEB_RUNTIME_ROOT/bref/ssl/cert.pem" \
        /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem; do
        if [[ -f "$bundle" && -r "$bundle" ]]; then
            export SSL_CERT_FILE="$bundle"
            break
        fi
    done
fi

# Configure after reading application INI so extra disabled functions survive.
# extension_dir is set on this first PHP invocation too: loading user-selected
# extensions must already work before the generated relocation INI exists.
# Disable CLI OPcache for this probe: its file cache can retain configuration-
# dependent ini_get results when a later startup uses different application INI.
"$runtime_php" -d "extension_dir=$BREF_WEB_RUNTIME_ROOT/bref/extensions" \
    -d opcache.enable_cli=0 "$compat_dir/configure.php"
export PHP_INI_SCAN_DIR="$scan_path:$HOME/conf.d"

cat > "$BREF_WEB_FPM_CONFIG" <<EOF
[global]
daemonize=no
pid=$HOME/php-fpm.pid
error_log=/proc/self/fd/2
log_limit=256000
log_buffering=no
process_control_timeout=1s

[default]
user=nobody
pm=static
pm.max_children=$BREF_WEB_MAX_IN_FLIGHT
pm.max_requests=$BREF_WEB_FPM_MAX_REQUESTS
listen=$BREF_WEB_FPM_ADDR
listen.allowed_clients=127.0.0.1
listen.backlog=128
clear_env=no
catch_workers_output=yes
decorate_workers_output=no
request_terminate_timeout=${BREF_WEB_FPM_TIMEOUT_SECONDS}s
request_terminate_timeout_track_finished=yes
security.limit_extensions=.php
rlimit_core=0
chdir="$LAMBDA_TASK_ROOT"
access.log=/proc/self/fd/2
access.format="bref_web_fpm request_id=%{LAMBDA_REQUEST_ID}e pid=%p status=%s duration_us=%{microseconds}d peak_bytes=%M"
EOF

printf 'bref_web_compat configured gateway=%s workers=%s timeout_seconds=%s\n' \
    "$BREF_WEB_GATEWAY" "$BREF_WEB_MAX_IN_FLIGHT" "$BREF_WEB_FPM_TIMEOUT_SECONDS" >&2

# PHP runs initialization and then replaces itself with the selected launcher.
# Container mode launches Node; a managed Node process can request Rust directly.
# Environment changes reach the supervisor without a plaintext IPC file.
# Neither this script nor init.php starts PHP-FPM: the selected supervisor owns it.
if (( $# == 0 )); then
    set -- "$BREF_WEB_NODE_BIN" "$BREF_WEB_LAUNCHER"
fi
exec "$runtime_php" "$compat_dir/init.php" "$@"
