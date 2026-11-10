#!/usr/bin/env bash
# Runs without AWS or networking, unprivileged with a read-only root, as on Lambda.
set -euo pipefail

if [[ "${1:-}" != --inside ]]; then
    compat_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    exec docker run --rm --network none \
        --user 1000:1000 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 \
        --entrypoint /bin/bash \
        --mount "type=bind,src=$compat_dir,dst=/var/task/compat,readonly" \
        "${BREF_WEB_TEST_IMAGE:-bref/php-84:3}" \
        /var/task/compat/tests.sh --inside
fi

# The launcher's first step: compat/bootstrap.php with no INI loaded.
bootstrap=(/opt/bin/php -n /var/task/compat/bootstrap.php)

fail() {
    printf 'compat test failed: %s\n' "$1" >&2
    exit 1
}

export BREF_WEB_MAX_IN_FLIGHT=4
export BREF_WEB_PHP_MEMORY_LIMIT=128M
export BREF_WEB_FPM_TIMEOUT_SECONDS=28
export BREF_AUTOLOAD_PATH=/tmp/compat-autoload.php
export BREF_WEB_INIT_FILE=/tmp/compat-init.php

cat > "$BREF_AUTOLOAD_PATH" <<'PHP'
<?php
namespace Bref;
putenv('COMPAT_TEST_ORDER=autoload');
final class LazySecretsLoader {
    public static function loadSecretEnvironmentVariables(): void {
        if (getenv('COMPAT_TEST_ORDER') !== 'autoload') {
            throw new \RuntimeException('Ordering failure');
        }
        putenv('COMPAT_TEST_SENSITIVE=fixture-sensitive-value');
        putenv('COMPAT_TEST_ORDER=autoload,secrets');
    }
}
final class Bref {
    public static function events(): object {
        return new class {
            public function beforeStartup(): void {
                if (getenv('COMPAT_TEST_ORDER') !== 'autoload,secrets,init') {
                    throw new \RuntimeException('Ordering failure');
                }
                putenv('COMPAT_TEST_HOOK=1');
                putenv('COMPAT_TEST_ORDER=autoload,secrets,init,beforeStartup');
            }
        };
    }
}
PHP

cat > "$BREF_WEB_INIT_FILE" <<'PHP'
<?php
if (getenv('COMPAT_TEST_SENSITIVE') !== 'fixture-sensitive-value'
    || getenv('COMPAT_TEST_ORDER') !== 'autoload,secrets') {
    throw new RuntimeException('Ordering failure');
}
putenv('COMPAT_TEST_INIT=1');
putenv('COMPAT_TEST_ORDER=autoload,secrets,init');
PHP

cat > /tmp/compat-launcher.php <<'PHP'
<?php
$valid = getenv('COMPAT_TEST_SENSITIVE') === 'fixture-sensitive-value'
    && getenv('COMPAT_TEST_INIT') === '1'
    && getenv('COMPAT_TEST_HOOK') === '1'
    && getenv('COMPAT_TEST_ORDER') === 'autoload,secrets,init,beforeStartup'
    && ini_get('memory_limit') === '128M'
    && ini_get('opcache.file_cache_only') === '0'
    && in_array('fastcgi_finish_request', explode(',', ini_get('disable_functions')), true)
    && is_writable(ini_get('upload_tmp_dir'))
    && is_writable(session_save_path());
if (! $valid) {
    fwrite(STDERR, "compat launcher assertion failed\n");
    exit(1);
}
file_put_contents('/tmp/compat-launcher-ran', 'ok');
PHP

"${bootstrap[@]}" /opt/bin/php /tmp/compat-launcher.php
[[ -f /tmp/compat-launcher-ran ]] || fail launcher_did_not_execute
printf 'PASS initialization ordering, environment inheritance and INI overrides\n'

# Error text deliberately contains the fake sensitive value. The common helper
# must suppress it, stop initialization and never start the launcher.
cat > /tmp/compat-throw.php <<'PHP'
<?php
throw new RuntimeException('fixture-sensitive-value');
PHP

expect_initialization_failure() {
    local init_file="$1"
    local output status
    rm -f /tmp/compat-launcher-ran
    set +e
    output="$(BREF_WEB_INIT_FILE="$init_file" "${bootstrap[@]}" \
        /opt/bin/php /tmp/compat-launcher.php 2>&1)"
    status=$?
    set -e
    [[ "$status" == 78 ]] || fail initialization_failure_exit_code
    [[ "$output" == *initialization_failed* ]] || fail missing_failure_marker
    [[ "$output" =~ initialization_failed\ error_type=[A-Za-z\\]+\ at=[^[:space:]]+:[0-9]+ ]] \
        || fail failure_marker_without_location
    [[ "$output" != *fixture-sensitive-value* ]] || fail sensitive_exception_was_logged
    [[ ! -e /tmp/compat-launcher-ran ]] || fail launcher_ran_after_failure
}

expect_initialization_failure /tmp/does-not-exist.php
expect_initialization_failure /tmp/compat-throw.php
printf 'PASS missing/throwing initialization exits 78 without launching or logging sensitive text\n'

# Exercise application INI precedence and exact byte limits, not just defaults.
mkdir -p /tmp/compat-application-ini
cat > /tmp/compat-application-ini/application.ini <<'INI'
precision=11
memory_limit=99M
opcache.memory_consumption=64
post_max_size=2000000
upload_max_filesize=2000000
disable_functions=exec,passthru
INI
cat > /tmp/compat-config-assert.php <<'PHP'
<?php
$expected = json_decode($argv[1], true, flags: JSON_THROW_ON_ERROR);
foreach ($expected as $name => $value) {
    if ((string) ini_get($name) !== $value) {
        fwrite(STDERR, "compat INI assertion failed: $name\n");
        exit(1);
    }
}
$disabled = explode(',', (string) ini_get('disable_functions'));
if (! in_array('fastcgi_finish_request', $disabled, true)) {
    exit(1);
}
foreach (explode(',', $argv[2] ?? '') as $function) {
    if ($function !== '' && ! in_array($function, $disabled, true)) {
        fwrite(STDERR, "compat disabled function was lost: $function; effective=" . implode(',', $disabled) . "\n");
        exit(1);
    }
}
if (($argv[3] ?? '') === 'relocated') {
    $root = '/tmp/compat-runtime';
    if (getenv('BREF_WEB_RUNTIME_ROOT') !== $root
        || getenv('LAMBDA_TASK_ROOT') !== '/tmp/compat-task'
        || ! str_starts_with((string) getenv('PATH'), $root . '/bin:')
        || ! str_starts_with((string) getenv('LD_LIBRARY_PATH'), $root . '/lib:')
        || ini_get('extension_dir') !== $root . '/bref/extensions'
        || getenv('PHPRC') !== $root . '/bref/etc/php') {
        fwrite(STDERR, "compat runtime root assertion failed\n");
        exit(1);
    }
}
PHP

# As with Bref, directives set by application INI win over runtime defaults.
PHP_INI_SCAN_DIR=/opt/bref/etc/php/conf.d:/tmp/compat-application-ini \
    BREF_WEB_MAX_BODY_BYTES=1200001 \
    "${bootstrap[@]}" /opt/bin/php /tmp/compat-config-assert.php \
    '{"precision":"11","memory_limit":"99M","opcache.memory_consumption":"64","post_max_size":"2000000","upload_max_filesize":"2000000"}' \
    exec,passthru
printf 'PASS application INI wins over runtime defaults; custom disabled functions are merged\n'

# Directives the application leaves unset take the BREF_WEB_* budgets exactly,
# replacing the runtime's own values (Bref's memory_limit=10240M included).
cat > /tmp/compat-application-ini/application.ini <<'INI'
precision=11
INI
PHP_INI_SCAN_DIR=/opt/bref/etc/php/conf.d:/tmp/compat-application-ini \
    BREF_WEB_MAX_BODY_BYTES=1200001 BREF_WEB_PHP_MEMORY_LIMIT=77M BREF_WEB_OPCACHE_MEMORY_MB=96 \
    "${bootstrap[@]}" /opt/bin/php /tmp/compat-config-assert.php \
    '{"precision":"11","memory_limit":"77M","opcache.memory_consumption":"96","post_max_size":"1200001","upload_max_filesize":"1200001","output_buffering":"0"}'
printf 'PASS unset directives take BREF_WEB_* budgets and exact byte limits\n'

# A fresh configuration must not re-import a stale generated disabled-function
# list from a previous startup's PHP_INI_SCAN_DIR.
cat > /tmp/compat-application-ini/application.ini <<'INI'
precision=11
disable_functions=passthru
openssl.cafile=/tmp/compat-custom-ca.pem
curl.cainfo=/tmp/compat-custom-ca.pem
INI
cp /etc/pki/tls/certs/ca-bundle.crt /tmp/compat-custom-ca.pem
PHP_INI_SCAN_DIR=/opt/bref/etc/php/conf.d:/tmp/compat-application-ini:/tmp/bref-web/conf.d \
    "${bootstrap[@]}" /opt/bin/php /tmp/compat-config-assert.php \
    '{"precision":"11","openssl.cafile":"/tmp/compat-custom-ca.pem","curl.cainfo":"/tmp/compat-custom-ca.pem","post_max_size":"6291456","upload_max_filesize":"6291456"}' \
    passthru
if PHP_INI_SCAN_DIR=/opt/bref/etc/php/conf.d:/tmp/bref-web/conf.d \
    /opt/bin/php -r 'exit(function_exists("exec") && ! function_exists("passthru") ? 0 : 1);'; then
    :
else
    fail stale_disabled_function_was_retained
fi
printf 'PASS regenerated overrides discard stale state and preserve application TLS CA choices\n'

cat > /tmp/compat-application-ini/application.ini <<'INI'
extension=redis
session.save_handler=redis
session.save_path="tcp://127.0.0.1:6379"
disable_functions=passthru
INI
PHP_INI_SCAN_DIR=/opt/bref/etc/php/conf.d:/tmp/compat-application-ini \
    "${bootstrap[@]}" /opt/bin/php /tmp/compat-config-assert.php \
    '{"session.save_handler":"redis","session.save_path":"tcp://127.0.0.1:6379"}' passthru
printf 'PASS non-file session configuration survives without connecting to its backend\n'

# This verifies path derivation, not an isolated ELF relocation: /opt remains
# available here. The exact-archive tests provide the independent relocation proof.
ln -s /opt /tmp/compat-runtime
mkdir -p /tmp/compat-task
cp "$BREF_WEB_INIT_FILE" /tmp/compat-task/initialization.php
LAMBDA_TASK_ROOT=/tmp/compat-task BREF_WEB_RUNTIME_ROOT=/tmp/compat-runtime \
    BREF_WEB_INIT_FILE=initialization.php \
    PHP_INI_SCAN_DIR=/opt/bref/etc/php/conf.d:/tmp/compat-application-ini \
    "${bootstrap[@]}" /tmp/compat-runtime/bin/php /tmp/compat-config-assert.php \
    '{"post_max_size":"6291456","upload_max_filesize":"6291456"}' passthru relocated
printf 'PASS alternate runtime/task roots and relative application initialization\n'

for invalid_limit in 0 -1 1M 2147483648; do
    set +e
    output="$(BREF_WEB_MAX_BODY_BYTES="$invalid_limit" "${bootstrap[@]}" \
        /opt/bin/php /tmp/compat-launcher.php 2>&1)"
    status=$?
    set -e
    [[ "$status" == 78 && "$output" == *configuration_error=max_body_bytes* ]] \
        || fail invalid_body_limit_was_not_rejected
done
printf 'PASS invalid or overflowing body limits fail configuration before startup\n'

expect_configuration_error() {
    local reason="$1" output status
    shift
    set +e
    output="$(env "$@" "${bootstrap[@]}" /opt/bin/php /tmp/compat-launcher.php 2>&1)"
    status=$?
    set -e
    [[ "$status" == 78 && "$output" == *"configuration_error=$reason"* ]] || fail "expected_$reason"
}
expect_configuration_error max_in_flight_exceeds_128 BREF_WEB_MAX_IN_FLIGHT=129
expect_configuration_error max_in_flight_must_be_a_positive_integer BREF_WEB_MAX_IN_FLIGHT=0
expect_configuration_error fpm_timeout_must_be_a_positive_integer BREF_WEB_FPM_TIMEOUT_SECONDS=1.5
expect_configuration_error php_memory_limit_must_be_positive_bytes_or_uppercase_K_M_G BREF_WEB_PHP_MEMORY_LIMIT=128m
expect_configuration_error fpm_addr_must_be_loopback_ipv4_and_port BREF_WEB_FPM_ADDR=0.0.0.0:9000
expect_configuration_error fpm_port_out_of_range BREF_WEB_FPM_ADDR=127.0.0.1:65536
expect_configuration_error task_root_must_be_a_safe_absolute_path LAMBDA_TASK_ROOT=relative
expect_configuration_error php_binary_is_not_executable BREF_WEB_RUNTIME_ROOT=/tmp/no-runtime
printf 'PASS invalid worker, timeout, memory, FPM address and path settings fail before startup\n'

set +e
output="$("${bootstrap[@]}" 2>&1)"
status=$?
set -e
[[ "$status" == 78 && "$output" == *configuration_error=usage* ]] || fail bootstrap_ran_without_executable
printf 'PASS bootstrap requires the executable it hands over to\n'

env -u BREF_WEB_MAX_IN_FLIGHT "${bootstrap[@]}" /opt/bin/php /tmp/compat-launcher.php
grep -qx 'pm.max_children=12' /tmp/bref-web/php-fpm.conf || fail default_worker_count_is_not_12
printf 'PASS twelve FPM workers by default\n'

# Restore the original /opt/default application configuration before FPM checks.
"${bootstrap[@]}" /opt/bin/php /tmp/compat-launcher.php

# Also start and stop the generated static pool, rather than only linting it.
export PHP_INI_SCAN_DIR=/opt/bref/etc/php/conf.d:/tmp/bref-web/conf.d
/opt/bin/php-fpm -t -y /tmp/bref-web/php-fpm.conf -d opcache.file_cache_only=0
fpm_pid=''
cleanup() {
    if [[ -n "$fpm_pid" ]]; then
        kill -TERM "$fpm_pid" 2>/dev/null || true
        wait "$fpm_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT
/opt/bin/php-fpm -F -O -y /tmp/bref-web/php-fpm.conf \
    -d opcache.file_cache_only=0 >/tmp/compat-fpm.log 2>&1 &
fpm_pid=$!
pool_ready=0
for (( attempt=0; attempt<100; attempt++ )); do
    if /opt/bin/php -r '
        $pid = (int) $argv[1];
        $count = 0;
        foreach (glob("/proc/[0-9]*/status") as $statusPath) {
            $status = (string) @file_get_contents($statusPath);
            if (preg_match("/^PPid:\\s+" . $pid . "$/m", $status)) { $count++; }
        }
        $socket = @stream_socket_client("tcp://127.0.0.1:9000", $errno, $error, 0.05);
        $connected = is_resource($socket);
        if ($connected) { fclose($socket); }
        exit($count === 4 && $connected ? 0 : 1);
    ' "$fpm_pid"; then
        pool_ready=1
        break
    fi
    sleep 0.02
done
if [[ "$pool_ready" != 1 ]]; then
    cat /tmp/compat-fpm.log >&2
    fail four_worker_pool_not_ready
fi
kill -QUIT "$fpm_pid"
wait "$fpm_pid"
fpm_pid=''
trap - EXIT
printf 'PASS static four-worker FPM pool startup and graceful shutdown\n'
