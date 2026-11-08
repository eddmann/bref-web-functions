<?php declare(strict_types=1);

/** Generate the final runtime INI after the application configuration was read. */
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

    // These are explicit runtime invariants. They override earlier application
    // INI, with memory/OPcache/body sizes controlled by BREF_WEB_* settings.
    $settings = [
        'extension_dir' => $runtimeRoot . '/bref/extensions',
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
        'disable_functions' => implode(',', array_keys($disabled)),
    ];
    // Preserve application configuration for non-file session handlers.
    if (ini_get('session.save_handler') === 'files') {
        $settings['session.save_path'] = $home . '/sessions';
    }
    $bundle = getenv('SSL_CERT_FILE');
    if (is_string($bundle) && is_file($bundle) && is_readable($bundle)) {
        foreach (['openssl.cafile', 'curl.cainfo'] as $directive) {
            if (ini_get($directive) === '') {
                $settings[$directive] = $bundle;
            }
        }
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
