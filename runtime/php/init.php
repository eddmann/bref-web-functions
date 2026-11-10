<?php declare(strict_types=1);

/**
 * Environment initialization before the gateway starts, deliberately separate
 * from Bref's event runtime. No application HTTP handler or framework kernel is
 * booted here.
 *
 * Runs Bref's secrets loader and beforeStartup, then execs the given executable
 * (the Rust gateway). afterStartup and the invocation hooks are NOT supported:
 * PHP-FPM is not running yet, and each HTTP request runs in its own PHP-FPM
 * request lifecycle.
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
        throw new RuntimeException('The gateway must be an absolute executable path');
    }
    if (! function_exists('pcntl_exec')) {
        throw new RuntimeException('pcntl_exec is required to preserve the initialization environment');
    }

    fwrite(STDERR, "bref_web_compat initialized startup_hooks=before_only\n");
    pcntl_exec($executable, $command);
    throw new RuntimeException('Could not execute the gateway');
} catch (Throwable $error) {
    // Exception messages and traces can include resolved secrets. Keep the common
    // initialization failure marker useful without logging environment contents:
    // the class and where it was thrown (e.g. Bref's secrets loader asking for
    // bref/secrets-loader) usually name the cause.
    $root = rtrim(getenv('LAMBDA_TASK_ROOT') ?: '/var/task', '/') . '/';
    $file = str_starts_with($error->getFile(), $root) ? substr($error->getFile(), strlen($root)) : $error->getFile();
    fwrite(STDERR, sprintf("bref_web_compat initialization_failed error_type=%s at=%s:%d\n",
        get_class($error), $file, $error->getLine()));
    exit(78);
}
