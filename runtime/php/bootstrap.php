<?php declare(strict_types=1);

/**
 * First startup step, run as `php -n bootstrap.php <executable> [args...]`.
 *
 * Validates the BREF_WEB_* settings, derives the paths of Bref's PHP
 * distribution (at /opt, or relocated into a deployment archive under
 * BREF_WEB_RUNTIME_ROOT), writes the PHP-FPM pool configuration, then execs PHP
 * with the application's INI loaded to generate the runtime INI (configure.php),
 * which in turn execs init.php and finally <executable>, the gateway.
 *
 * `-n` keeps application INI out of this step: it may load extensions from a
 * directory that is only configured once the runtime's paths are known.
 */

const CONFIGURATION_ERROR = 78;

function fail(string $reason): never
{
    fwrite(STDERR, "bref_web_compat configuration_error=$reason\n");
    exit(CONFIGURATION_ERROR);
}

/** The variable's value, or $default when it is unset or empty; exported either way. */
function setting(string $name, string $default): string
{
    $value = getenv($name);
    $value = $value === false || $value === '' ? $default : $value;
    putenv("$name=$value");
    return $value;
}

function export(string $name, string $value): void
{
    putenv("$name=$value");
}

function positive_integer(string $name, string $value): int
{
    preg_match('/\A[1-9][0-9]{0,6}\z/', $value) || fail("{$name}_must_be_a_positive_integer");
    return (int) $value;
}

function absolute_path(string $name, string $value): void
{
    (str_starts_with($value, '/') && strpbrk($value, "\n\r\"") === false)
        || fail("{$name}_must_be_a_safe_absolute_path");
}

$command = array_slice($argv, 1);
$command !== [] || fail('usage_bootstrap_php_executable_args');
umask(077);

$taskRoot = setting('LAMBDA_TASK_ROOT', '/var/task');
$runtimeRoot = setting('BREF_WEB_RUNTIME_ROOT', '/opt');
$home = '/tmp/bref-web';
export('HOME', $home);
export('PATH', "$runtimeRoot/bin:" . (string) getenv('PATH'));
$libraries = "$runtimeRoot/lib:$runtimeRoot/lib64";
$inherited = (string) getenv('LD_LIBRARY_PATH');
// The launcher already sets the runtime's libraries so this PHP could start.
if (! str_starts_with($inherited, $libraries)) {
    export('LD_LIBRARY_PATH', $libraries . ($inherited === '' ? '' : ":$inherited"));
}
$phprc = setting('PHPRC', "$runtimeRoot/bref/etc/php");
export('BREF_RUNTIME', 'fpm');
$workers = setting('BREF_WEB_MAX_IN_FLIGHT', '12');
$fpmTimeout = setting('BREF_WEB_FPM_TIMEOUT_SECONDS', '28');
$maxRequests = setting('BREF_WEB_FPM_MAX_REQUESTS', '100');
$memoryLimit = setting('BREF_WEB_PHP_MEMORY_LIMIT', '128M');
$opcacheMemory = setting('BREF_WEB_OPCACHE_MEMORY_MB', '128');
$maxBody = setting('BREF_WEB_MAX_BODY_BYTES', '6291456');
$fpmAddress = setting('BREF_WEB_FPM_ADDR', '127.0.0.1:9000');
$fpmBinary = setting('BREF_WEB_FPM_BIN', "$runtimeRoot/bin/php-fpm");
$fpmConfig = setting('BREF_WEB_FPM_CONFIG', "$home/php-fpm.conf");

positive_integer('max_in_flight', $workers) <= 128 || fail('max_in_flight_exceeds_128');
positive_integer('fpm_timeout', $fpmTimeout);
positive_integer('max_requests', $maxRequests);
positive_integer('opcache_memory', $opcacheMemory);
preg_match('/\A[1-9][0-9]{0,9}\z/', $maxBody) || fail('max_body_bytes_must_be_a_positive_integer');
(int) $maxBody <= 2147483647 || fail('max_body_bytes_exceeds_2147483647');
absolute_path('task_root', $taskRoot);
absolute_path('runtime_root', $runtimeRoot);
absolute_path('php_configuration', $phprc);
$php = "$runtimeRoot/bin/php";
is_executable($php) || fail('php_binary_is_not_executable');
preg_match('/\A[1-9][0-9]{0,9}[KMG]?\z/', $memoryLimit)
    || fail('php_memory_limit_must_be_positive_bytes_or_uppercase_K_M_G');
preg_match('/\A127\.0\.0\.1:([1-9][0-9]{0,4})\z/', $fpmAddress, $port)
    || fail('fpm_addr_must_be_loopback_ipv4_and_port');
(int) $port[1] <= 65535 || fail('fpm_port_out_of_range');
absolute_path('fpm_config', $fpmConfig);
is_executable($fpmBinary) || fail('fpm_binary_is_not_executable');

foreach (["$home/conf.d", "$home/tmp", "$home/sessions", dirname($fpmConfig)] as $directory) {
    is_dir($directory) || mkdir($directory, 0700, true) || fail('could_not_create_' . basename($directory));
}
export('TMPDIR', "$home/tmp");

// Preserve application scan directories, always include the runtime's defaults,
// and skip a previously generated override while reading application INI. The
// original /opt/bref/etc/php/conf.d maps to the relocated runtime, so an
// existing PHP_INI_SCAN_DIR keeps working without loading extensions twice.
$runtimeScan = "$runtimeRoot/bref/etc/php/conf.d";
$scan = [$runtimeScan];
$applicationScan = (string) getenv('PHP_INI_SCAN_DIR');
foreach (explode(':', $applicationScan === '' ? "$taskRoot/php/conf.d" : $applicationScan) as $directory) {
    if ($directory === '/opt/bref/etc/php/conf.d' && $runtimeRoot !== '/opt') {
        $directory = $runtimeScan;
    }
    if ($directory !== '' && $directory !== "$home/conf.d" && ! in_array($directory, $scan, true)) {
        $scan[] = $directory;
    }
}
export('PHP_INI_SCAN_DIR', implode(':', $scan));

if ((string) getenv('SSL_CERT_FILE') === '') {
    foreach (["$runtimeRoot/bref/ssl/cert.pem", '/etc/pki/tls/certs/ca-bundle.crt',
        '/etc/ssl/certs/ca-certificates.crt', '/etc/ssl/cert.pem'] as $bundle) {
        if (is_file($bundle) && is_readable($bundle)) {
            export('SSL_CERT_FILE', $bundle);
            break;
        }
    }
}

$pool = <<<CONF
[global]
daemonize=no
pid=$home/php-fpm.pid
error_log=/proc/self/fd/2
log_limit=256000
log_buffering=no
process_control_timeout=1s

[default]
user=nobody
pm=static
pm.max_children=$workers
pm.max_requests=$maxRequests
listen=$fpmAddress
listen.allowed_clients=127.0.0.1
listen.backlog=128
clear_env=no
catch_workers_output=yes
decorate_workers_output=no
request_terminate_timeout={$fpmTimeout}s
request_terminate_timeout_track_finished=yes
security.limit_extensions=.php
rlimit_core=0
chdir="$taskRoot"
access.log=/proc/self/fd/2
access.format="bref_web_fpm request_id=%{LAMBDA_REQUEST_ID}e pid=%p status=%s duration_us=%{microseconds}d peak_bytes=%M"

CONF;
file_put_contents($fpmConfig, $pool) === strlen($pool) || fail('could_not_write_fpm_config');

// extension_dir is set here too: application INI may load extensions, and the
// generated INI that sets it does not exist yet. CLI OPcache is off so its file
// cache cannot return INI values from a run with different application INI.
pcntl_exec($php, ['-d', "extension_dir=$runtimeRoot/bref/extensions", '-d', 'opcache.enable_cli=0',
    __DIR__ . '/configure.php', ...$command], getenv());
fail('could_not_execute_php');
