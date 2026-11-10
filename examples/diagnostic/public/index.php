<?php declare(strict_types=1);

// Deployable diagnostics for the AWS exploration. Reports what this runtime
// receives from the ingress and which environment answered. Non-destructive:
// it writes only a per-environment marker under /tmp and never echoes
// credential values (authorization, cookies, AWS keys).

$started = microtime(true);

// One marker per execution environment, shared by its FPM workers. Exclusive
// creation means exactly one worker records the environment's first request.
$markerDirectory = '/tmp/bref-web-diagnostic';
$markerFile = $markerDirectory . '/environment.json';
$firstRequestInEnvironment = false;
if (! is_dir($markerDirectory)) {
    @mkdir($markerDirectory, 0700, true);
}

// PHP state resets between FPM requests, so count per worker PID on disk.
$counterFile = $markerDirectory . '/worker-' . getmypid() . '.count';
$workerRequests = (int) @file_get_contents($counterFile) + 1;
@file_put_contents($counterFile, (string) $workerRequests);
$handle = @fopen($markerFile, 'x');
if ($handle !== false) {
    $firstRequestInEnvironment = true;
    flock($handle, LOCK_EX);
    fwrite($handle, json_encode([
        'environment_id' => bin2hex(random_bytes(8)),
        'first_request_at' => gmdate('Y-m-d\TH:i:s.v\Z'),
    ], JSON_THROW_ON_ERROR));
    flock($handle, LOCK_UN);
    fclose($handle);
}
// Another worker may have created the marker but not yet locked or written it.
$environment = [];
for ($attempt = 0; $attempt < 20 && $environment === []; $attempt++) {
    $reader = @fopen($markerFile, 'r');
    if ($reader !== false) {
        flock($reader, LOCK_SH);
        $environment = json_decode((string) stream_get_contents($reader), true) ?: [];
        fclose($reader);
    }
    if ($environment === []) {
        usleep(1000);
    }
}

// The runtime writes its FPM configuration once at startup.
$configuredAt = @filemtime('/tmp/bref-web/php-fpm.conf');

$redacted = ['HTTP_AUTHORIZATION', 'HTTP_COOKIE', 'HTTP_PROXY_AUTHORIZATION', 'HTTP_X_AWS_TEST_TOKEN'];
$headers = [];
foreach ($_SERVER as $name => $value) {
    if (! str_starts_with($name, 'HTTP_')) {
        continue;
    }
    $headers[strtolower(str_replace('_', '-', substr($name, 5)))] = in_array($name, $redacted, true)
        ? '[redacted ' . strlen((string) $value) . ' bytes]'
        : $value;
}
ksort($headers);

$cgi = [];
foreach ([
    'REQUEST_METHOD', 'REQUEST_URI', 'QUERY_STRING', 'SERVER_PROTOCOL', 'HTTPS', 'REQUEST_SCHEME',
    'SERVER_NAME', 'SERVER_PORT', 'SERVER_ADDR', 'REMOTE_ADDR', 'REMOTE_PORT', 'CONTENT_TYPE',
    'CONTENT_LENGTH', 'LAMBDA_REQUEST_ID', '_X_AMZN_TRACE_ID', 'SCRIPT_NAME', 'PATH_INFO',
] as $name) {
    $cgi[$name] = $_SERVER[$name] ?? null;
}

// Process environment, never credential values. Match by name so new secrets
// stay hidden: AWS keys, AWS_LAMBDA_METADATA_TOKEN (fetches credentials inside
// the environment) and AWS_TEST_TOKEN (guards the test-only routes in tests/aws).
// The account ID is not a secret, but this page is public.
$lambda = [];
foreach ((array) getenv(local_only: true) as $name => $value) {
    if (preg_match('/KEY|SECRET|TOKEN|SESSION|PASSWORD|CREDENTIAL/', $name) || $name === 'AWS_ACCOUNT_ID') {
        continue;
    }
    if (str_starts_with($name, 'AWS_') || str_starts_with($name, 'LAMBDA_') || str_starts_with($name, 'BREF_')
        || in_array($name, ['_HANDLER', 'TZ', 'PATH'], true)) {
        $lambda[$name] = $value;
    }
}
ksort($lambda);

header('Content-Type: application/json');
header('Cache-Control: no-store');
echo json_encode([
    'ok' => true,
    'request_id' => $_SERVER['LAMBDA_REQUEST_ID'] ?? null,
    'php' => PHP_VERSION,
    'sapi' => PHP_SAPI,
    'architecture' => php_uname('m'),
    'environment' => $environment + [
        'first_request_in_environment' => $firstRequestInEnvironment,
        'runtime_configured_at' => $configuredAt === false ? null : gmdate('Y-m-d\TH:i:s\Z', $configuredAt),
    ],
    'worker' => [
        'pid' => getmypid(),
        'requests_served' => $workerRequests,
        'memory_limit' => ini_get('memory_limit'),
        'peak_memory_bytes' => memory_get_peak_usage(true),
    ],
    'time' => gmdate('Y-m-d\TH:i:s.v\Z'),
    'cgi' => $cgi,
    'headers' => $headers,
    'process_environment' => $lambda,
    'aws_credentials_present' => getenv('AWS_ACCESS_KEY_ID', true) !== false,
    'duration_ms' => round((microtime(true) - $started) * 1000, 3),
], JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT), "\n";
