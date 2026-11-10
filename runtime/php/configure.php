<?php declare(strict_types=1);

/**
 * Second startup step, exec'd by bootstrap.php with the application's INI
 * loaded: generates the runtime INI, then execs init.php with it added.
 */
try {
    $runtimeRoot = (string) getenv('BREF_WEB_RUNTIME_ROOT');
    $home = (string) getenv('HOME');
    $disabled = [];
    foreach (preg_split('/[,\s]+/', (string) ini_get('disable_functions'), -1, PREG_SPLIT_NO_EMPTY) as $function) {
        if (! preg_match('/\A[a-zA-Z_][a-zA-Z0-9_]*\z/', $function)) {
            throw new RuntimeException('Invalid disabled function name');
        }
        $disabled[strtolower($function)] = true;
    }
    $disabled['fastcgi_finish_request'] = true;

    // Invariants: the relocated extension directory, and fastcgi_finish_request
    // staying disabled (it would report completion while PHP keeps a worker busy,
    // breaking the gateway's worker accounting). Application disabled functions
    // are merged, never dropped.
    $forced = [
        'extension_dir' => $runtimeRoot . '/bref/extensions',
        'disable_functions' => implode(',', array_keys($disabled)),
    ];
    // Defaults. As with Bref, a directive set by application INI (PHPRC or
    // PHP_INI_SCAN_DIR outside the runtime's own configuration) wins; the
    // BREF_WEB_* budgets only replace the runtime's values.
    $defaults = [
        'display_errors' => '0',
        'log_errors' => '1',
        'error_log' => '/proc/self/fd/2',
        'memory_limit' => (string) getenv('BREF_WEB_PHP_MEMORY_LIMIT'),
        'opcache.enable' => '1',
        'opcache.enable_cli' => '1',
        'opcache.file_cache_only' => '0',
        'opcache.file_cache' => $home . '/tmp',
        'opcache.memory_consumption' => (string) getenv('BREF_WEB_OPCACHE_MEMORY_MB'),
        'opcache.max_accelerated_files' => '10000',
        'variables_order' => 'EGPCS',
        'post_max_size' => (string) getenv('BREF_WEB_MAX_BODY_BYTES'),
        'upload_max_filesize' => (string) getenv('BREF_WEB_MAX_BODY_BYTES'),
        'upload_tmp_dir' => $home . '/tmp',
        'sys_temp_dir' => $home . '/tmp',
        'session.upload_progress.enabled' => '0',
        'output_buffering' => '0',
        'zlib.output_compression' => '0',
    ];
    // Preserve application configuration for non-file session handlers.
    if (ini_get('session.save_handler') === 'files') {
        $defaults['session.save_path'] = $home . '/sessions';
    }
    $bundle = getenv('SSL_CERT_FILE');
    if (is_string($bundle) && is_file($bundle) && is_readable($bundle)) {
        foreach (['openssl.cafile', 'curl.cainfo'] as $directive) {
            if (ini_get($directive) === '') {
                $defaults[$directive] = $bundle;
            }
        }
    }

    $runtimeConfiguration = realpath($runtimeRoot . '/bref/etc/php');
    $applicationFiles = [];
    $loaded = php_ini_loaded_file();
    if (is_string($loaded)) {
        $applicationFiles[] = $loaded;
    }
    foreach (explode(',', (string) php_ini_scanned_files()) as $file) {
        if (trim($file) !== '') {
            $applicationFiles[] = trim($file);
        }
    }
    $applicationSet = [];
    foreach ($applicationFiles as $file) {
        $real = realpath($file);
        if ($real === false || ($runtimeConfiguration !== false
            && str_starts_with($real, $runtimeConfiguration . DIRECTORY_SEPARATOR))) {
            continue;
        }
        // Raw scanning only collects directive names; PHP already applied the values.
        $directives = @parse_ini_file($real, false, INI_SCANNER_RAW);
        foreach (is_array($directives) ? array_keys($directives) : [] as $name) {
            $applicationSet[(string) $name] = true;
        }
    }
    $kept = array_keys(array_intersect_key($defaults, $applicationSet));
    $settings = $forced + array_diff_key($defaults, $applicationSet);
    if ($kept !== []) {
        fwrite(STDERR, 'bref_web_compat application_ini=' . implode(',', $kept) . "\n");
    }

    $text = "; Generated last by Bref Web compatibility initialization.\n";
    foreach ($settings as $name => $value) {
        if (str_contains($value, "\n") || str_contains($value, "\r")) {
            throw new RuntimeException('Invalid INI value');
        }
        $text .= $name . '="' . addcslashes($value, "\\\"") . "\"\n";
    }
    $destination = $home . '/conf.d/zz-bref-web.ini';
    $temporary = tempnam(dirname($destination), '.bref-web-ini-');
    if ($temporary === false || file_put_contents($temporary, $text) !== strlen($text)
        || ! rename($temporary, $destination)) {
        throw new RuntimeException('Could not write runtime configuration');
    }
} catch (Throwable $error) {
    fwrite(STDERR, 'bref_web_compat configuration_error=php_ini_generation_failed error_type=' . get_class($error) . "\n");
    exit(78);
}

fwrite(STDERR, sprintf("bref_web_compat configured workers=%s timeout_seconds=%s\n",
    getenv('BREF_WEB_MAX_IN_FLIGHT'), getenv('BREF_WEB_FPM_TIMEOUT_SECONDS')));
putenv('PHP_INI_SCAN_DIR=' . getenv('PHP_INI_SCAN_DIR') . ':' . $home . '/conf.d');
// init.php replaces itself with the gateway, so environment changes made during
// initialization reach it without a plaintext IPC file. Neither step starts
// PHP-FPM: the gateway owns it.
pcntl_exec($runtimeRoot . '/bin/php', [__DIR__ . '/init.php', ...array_slice($argv, 1)], getenv());
fwrite(STDERR, "bref_web_compat configuration_error=could_not_execute_php\n");
exit(78);
