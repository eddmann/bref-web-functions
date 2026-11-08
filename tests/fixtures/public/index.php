<?php declare(strict_types=1);

// Local integration fixtures. This deliberately needs no application or Bref vendor files.
// Never install these routes in a production application.

function respondJson(array $value, int $status = 200): never
{
    http_response_code($status);
    header('Content-Type: application/json');
    echo json_encode($value, JSON_THROW_ON_ERROR | JSON_INVALID_UTF8_SUBSTITUTE);
    exit;
}

function requestHeaders(): array
{
    $headers = [];
    foreach ($_SERVER as $key => $value) {
        if (str_starts_with($key, 'HTTP_')) {
            $headers[strtolower(str_replace('_', '-', substr($key, 5)))] = $value;
        }
    }
    foreach (['CONTENT_TYPE' => 'content-type', 'CONTENT_LENGTH' => 'content-length'] as $key => $name) {
        if (isset($_SERVER[$key])) {
            $headers[$name] = $_SERVER[$key];
        }
    }
    ksort($headers);
    return $headers;
}

function requestId(): ?string
{
    return $_SERVER['LAMBDA_REQUEST_ID'] ?? null;
}

function isolation(): never
{
    static $staticCounter = 0;
    $previous = $GLOBALS['fixture_context'] ?? null;
    $GLOBALS['fixture_context'] = requestId();
    $GLOBALS['fixture_counter'] = ($GLOBALS['fixture_counter'] ?? 0) + 1;
    $staticCounter++;
    $delay = max(0, min(800, (int) ($_GET['sleep_ms'] ?? 0)));
    usleep($delay * 1000);
    respondJson([
        'pid' => getmypid(),
        'static_counter' => $staticCounter,
        'global_counter' => $GLOBALS['fixture_counter'],
        'previous_context' => $previous,
        'context' => $GLOBALS['fixture_context'],
        'request_id' => requestId(),
        'id' => $_GET['id'] ?? null,
    ]);
}

$uri = $_SERVER['REQUEST_URI'] ?? '/';
$path = explode('?', $uri, 2)[0];

if ($path === '/health') {
    respondJson(['ok' => true, 'pid' => getmypid(), 'request_id' => requestId()]);
}

if ($path === '/application-411') {
    // This is an intentional application status, not a gateway framing failure.
    // The chunked-PUT regression also verifies the body reached PHP intact.
    $body = file_get_contents('php://input');
    http_response_code(411);
    header('Content-Type: text/plain');
    header('X-Fixture-Application: intentional-411');
    header('X-Fixture-Method: ' . $_SERVER['REQUEST_METHOD']);
    header('X-Fixture-Body-Sha256: ' . hash('sha256', $body));
    echo "fixture application 411\n";
    exit;
}

if ($path === '/echo' || str_starts_with($path, '/echo/')) {
    $body = file_get_contents('php://input');
    $files = [];
    foreach ($_FILES as $name => $file) {
        if (! is_array($file) || is_array($file['tmp_name'])) {
            continue;
        }
        $files[$name] = [
            'name' => $file['name'], 'type' => $file['type'],
            'size' => $file['size'], 'error' => $file['error'],
            'sha256' => is_uploaded_file($file['tmp_name']) ? hash_file('sha256', $file['tmp_name']) : null,
        ];
    }
    $cgi = [];
    foreach (['REQUEST_METHOD', 'REQUEST_URI', 'QUERY_STRING', 'PATH_INFO', 'SCRIPT_FILENAME',
        'DOCUMENT_ROOT', 'SERVER_PROTOCOL', 'SERVER_NAME', 'SERVER_PORT', 'REMOTE_ADDR',
        'HTTPS', 'LAMBDA_TASK_ROOT', '_HANDLER', 'LAMBDA_REQUEST_ID', 'LAMBDA_INVOCATION_CONTEXT',
        'LAMBDA_REQUEST_CONTEXT', '_X_AMZN_TRACE_ID'] as $name) {
        $cgi[$name] = $_SERVER[$name] ?? null;
    }
    respondJson([
        'method' => $_SERVER['REQUEST_METHOD'], 'raw_uri' => $uri,
        'query_string' => $_SERVER['QUERY_STRING'] ?? '', 'cgi' => $cgi,
        'headers' => requestHeaders(), 'request_id' => requestId(),
        'get' => $_GET, 'post' => $_POST, 'files' => $files,
        'body_size' => strlen($body), 'body_sha256' => hash('sha256', $body),
        'pid' => getmypid(),
    ]);
}

if ($path === '/binary') {
    header('Content-Type: application/octet-stream');
    $bytes = implode('', array_map(chr(...), range(0, 255)));
    echo str_repeat($bytes, 16);
    exit;
}

if ($path === '/cookies') {
    header('Set-Cookie: first=one; Path=/; HttpOnly', false);
    header('Set-Cookie: second=two; Path=/; SameSite=Lax', false);
    respondJson(['ok' => true]);
}

if ($path === '/stream') {
    header('Content-Type: application/octet-stream');
    header('Cache-Control: no-cache');
    header('X-Accel-Buffering: no');
    while (ob_get_level() > 0) {
        ob_end_flush();
    }
    ob_implicit_flush(true);
    foreach ([1, 2, 3] as $number) {
        if ($number > 1) {
            usleep(200000);
        }
        echo "chunk-$number\n";
        flush();
    }
    exit;
}

if ($path === '/isolation') {
    isolation();
}

if ($path === '/hang') {
    sleep(5);
    respondJson(['unexpected_completion' => true, 'pid' => getmypid()]);
}

if ($path === '/disconnect-work') {
    // TEST ONLY: deliberately keep this PHP worker occupied after its client leaves.
    // Real applications should normally cancel unnecessary work on disconnect.
    ignore_user_abort(true);
    header('Content-Type: text/plain');
    header('Cache-Control: no-cache');
    header('X-Accel-Buffering: no');
    header('X-Fixture-Application: disconnect-work-test-only');
    while (ob_get_level() > 0) {
        ob_end_flush();
    }
    ob_implicit_flush(true);
    echo "disconnect-ack\n";
    flush();
    usleep(1000000);
    echo "disconnect-done\n";
    flush();
    exit;
}

if ($path === '/crash') {
    if (! function_exists('posix_kill')) {
        respondJson(['unsupported' => 'The fixture requires the PHP posix extension for a real worker crash.'], 501);
    }
    posix_kill(getmypid(), 9);
    respondJson(['unexpected_completion' => true], 500);
}

if ($path === '/fatal') {
    trigger_error('Intentional local fixture fatal error', E_USER_ERROR);
    exit;
}

if ($path === '/large-response') {
    header('Content-Type: application/octet-stream');
    header('Content-Length: 4194304');
    $block = str_repeat(implode('', array_map(chr(...), range(0, 255))), 256);
    for ($i = 0; $i < 64; $i++) {
        echo $block;
    }
    exit;
}

if ($path === '/session') {
    $directory = '/tmp/bref-web-prototype-sessions';
    if (! is_dir($directory) && ! mkdir($directory, 0700, true) && ! is_dir($directory)) {
        respondJson(['error' => 'Cannot create local fixture session directory'], 500);
    }
    session_save_path($directory);
    session_name('BREFWEBFIXTURE');
    session_cache_limiter('');
    session_start(['cookie_httponly' => true, 'cookie_samesite' => 'Lax']);
    $count = $_SESSION['count'] = ($_SESSION['count'] ?? 0) + 1;
    session_write_close();
    respondJson(['count' => $count, 'pid' => getmypid()]);
}

respondJson(['error' => 'Unknown local fixture route'], 404);
