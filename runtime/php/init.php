<?php declare(strict_types=1);

/**
 * Prototype environment initialization, deliberately separate from Bref's event
 * runtime. No application HTTP handler or framework kernel is booted here.
 *
 * beforeStartup is an initialization compatibility aid. afterStartup and the
 * invocation hooks are NOT preserved: FPM/gateway are not ready yet, and later
 * HTTP requests execute in independent PHP-FPM request lifecycles. The selected
 * executable can be Node in container mode or Rust under managed Node startup.
 */

try {
    $taskRoot = getenv('LAMBDA_TASK_ROOT') ?: '/var/task';
    $autoload = getenv('BREF_AUTOLOAD_PATH') ?: $taskRoot . '/vendor/autoload.php';

    if (is_file($autoload)) {
        require $autoload;
    } elseif (getenv('BREF_AUTOLOAD_PATH')) {
        throw new RuntimeException('Configured autoloader does not exist');
    }

    if (class_exists(\Bref\LazySecretsLoader::class)) {
        \Bref\LazySecretsLoader::loadSecretEnvironmentVariables();
    }

    // Optional application environment/filesystem initialization. To pass values
    // to the launcher/FPM, use putenv(); changing only $_ENV is insufficient.
    $initFile = getenv('BREF_WEB_INIT_FILE');
    if (is_string($initFile) && $initFile !== '') {
        if ($initFile[0] !== '/') {
            $initFile = $taskRoot . '/' . $initFile;
        }
        if (! is_file($initFile)) {
            throw new RuntimeException('Configured initialization file does not exist');
        }
        require $initFile;
    }

    if (class_exists(\Bref\Bref::class)) {
        \Bref\Bref::events()->beforeStartup();
    }

    $command = array_slice($argv, 1);
    $executable = array_shift($command);
    if (! is_string($executable) || $executable === '' || $executable[0] !== '/' || ! is_executable($executable)) {
        throw new RuntimeException('Launcher must be an absolute executable path');
    }
    if (! function_exists('pcntl_exec')) {
        throw new RuntimeException('pcntl_exec is required to preserve the initialization environment');
    }

    fwrite(STDERR, "bref_web_compat initialized startup_hooks=before_only\n");
    pcntl_exec($executable, $command);
    throw new RuntimeException('Could not execute the launcher');
} catch (Throwable $error) {
    // Exception messages and traces can include resolved secrets. Keep the common
    // initialization failure marker useful without logging environment contents.
    fwrite(STDERR, 'bref_web_compat initialization_failed error_type=' . get_class($error) . "\n");
    exit(78);
}
