<?php declare(strict_types=1);

// TEST-ONLY routes for the AWS exploration. These deliberately hold workers,
// crash them, or make one that PHP-FPM cannot stop, which retires the runtime.
// Deploy only to an isolated test account. Built by tests/aws/build-app.sh,
// which places the deployable diagnostic page next to this file.

$path = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/';

function bounded(string $name, int $default, int $minimum, int $maximum): int
{
    $value = filter_var($_GET[$name] ?? $default, FILTER_VALIDATE_INT);
    return max($minimum, min($maximum, $value === false ? $default : $value));
}

function respond(array $data): never
{
    header('Content-Type: application/json');
    header('Cache-Control: no-store');
    echo json_encode($data + [
        'request_id' => $_SERVER['LAMBDA_REQUEST_ID'] ?? null,
        'pid' => getmypid(),
    ], JSON_THROW_ON_ERROR), "\n";
    exit;
}

function mark(string $route): void
{
    error_log(sprintf('aws_test route=%s request_id=%s pid=%d', $route, $_SERVER['LAMBDA_REQUEST_ID'] ?? '-', getmypid()));
}

// Deployments set AWS_TEST_TOKEN (infra/aws requires it) so only the tester
// can reach these routes on a public endpoint; the diagnostic page stays open.
$token = (string) getenv('AWS_TEST_TOKEN');
if (str_starts_with($path, '/aws-test/') && $token !== ''
    && ! hash_equals($token, (string) ($_SERVER['HTTP_X_AWS_TEST_TOKEN'] ?? ''))) {
    http_response_code(403);
    respond(['error' => 'missing or wrong X-Aws-Test-Token header']);
}

switch ($path) {
    case '/aws-test/sleep':
        // Below, at and beyond the HTTP (30 s) and FPM (28 s) deadlines.
        $milliseconds = bounded('ms', 1000, 0, 120000);
        mark('sleep');
        usleep($milliseconds * 1000);
        respond(['slept_ms' => $milliseconds]);

    case '/aws-test/stream':
        // Whether the ingress delivers chunks as written or buffers them.
        $chunks = bounded('chunks', 5, 1, 100);
        $interval = bounded('interval_ms', 500, 0, 5000);
        mark('stream');
        header('Content-Type: text/plain');
        header('Cache-Control: no-store');
        for ($chunk = 1; $chunk <= $chunks; $chunk++) {
            printf("chunk=%d at=%.3f\n", $chunk, microtime(true));
            flush();
            if ($chunk < $chunks) {
                usleep($interval * 1000);
            }
        }
        exit;

    case '/aws-test/large':
        // Response size limits and slow-reader behaviour through the ingress.
        $megabytes = bounded('mb', 8, 1, 64);
        mark('large');
        header('Content-Type: application/octet-stream');
        header('Content-Length: ' . $megabytes * 1048576);
        $block = str_repeat('x', 1048576);
        for ($written = 0; $written < $megabytes; $written++) {
            echo $block;
            flush();
        }
        exit;

    case '/aws-test/memory':
        // memory_limit (default 128M per worker) and environment memory.
        $megabytes = bounded('mb', 64, 1, 4096);
        mark('memory');
        $allocation = str_repeat('m', $megabytes * 1048576);
        // Holding the allocation keeps the worker busy, so enough concurrent
        // requests make Lambda fill every worker of an environment.
        usleep(bounded('hold_ms', 0, 0, 10000) * 1000);
        respond(['allocated_mb' => strlen($allocation) / 1048576]);

    case '/aws-test/crash':
        // A worker killed mid-request: the client gets 502, FPM replaces it.
        mark('crash');
        posix_kill(getmypid(), SIGKILL);
        respond(['unexpected' => 'worker survived SIGKILL']);

    case '/aws-test/stuck':
        // PHP-FPM ends an overrunning worker with SIGTERM. Ignoring it leaves
        // completion unconfirmed, so the gateway retires the whole runtime and
        // Lambda must replace the environment. One request is enough.
        $seconds = bounded('seconds', 90, 1, 300);
        mark('stuck');
        pcntl_signal(SIGTERM, SIG_IGN);
        $until = microtime(true) + $seconds;
        while (($remaining = $until - microtime(true)) > 0) {
            usleep((int) min(1_000_000, $remaining * 1_000_000));
        }
        respond(['unexpected' => 'stuck worker completed', 'seconds' => $seconds]);
}

require __DIR__ . '/diagnostic.php';
