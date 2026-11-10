<?php declare(strict_types=1);
// Dump what the application sees: request-scoped $_SERVER (not process env), superglobals, INI.
$server = [];
foreach ($_SERVER as $k => $v) {
    if ($k === 'REQUEST_TIME' || $k === 'REQUEST_TIME_FLOAT' || $k === 'argv' || $k === 'argc') continue;
    if (getenv($k, true) === $v && !str_starts_with($k, 'HTTP_')) continue;
    $server[$k] = $v;
}
ksort($server);
$ini = [];
foreach (['memory_limit','post_max_size','upload_max_filesize','max_execution_time','disable_functions','opcache.enable','opcache.validate_timestamps','output_buffering','display_errors','error_reporting','session.save_path','upload_tmp_dir','sys_temp_dir','opcache.file_cache','opcache.file_cache_only','variables_order','max_input_time'] as $n) $ini[$n] = ini_get($n);
header('Content-Type: application/json');
header('X-Multi: one'); header('X-Multi: two', false);
setcookie('c1', 'v1'); setcookie('c2', 'v2');
echo json_encode(['server' => $server, 'get' => $_GET, 'post' => $_POST, 'cookie' => $_COOKIE,
  'files' => array_map(fn($f) => $f['size'], $_FILES), 'input_len' => strlen(file_get_contents('php://input')),
  'ini' => $ini, 'extensions' => get_loaded_extensions(), 'sapi' => PHP_SAPI], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
