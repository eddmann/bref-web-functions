<?php declare(strict_types=1);

// Small non-destructive packaging probe. Production packages should supply an
// explicit application directory; this file is not the integration crash suite.
header('Content-Type: application/json');
echo json_encode([
    'ok' => true,
    'php' => PHP_VERSION,
    'sapi' => PHP_SAPI,
    'request_id' => $_SERVER['LAMBDA_REQUEST_ID'] ?? null,
], JSON_THROW_ON_ERROR), "\n";
