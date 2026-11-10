<?php declare(strict_types=1);

// This is a local compatibility fixture. It exposes runtime diagnostic fields
// and must not be used as a production application's front controller.
require dirname(__DIR__) . '/vendor/autoload.php';

use Bref\Bref;
use BrefWebFixture\CompatibilitySubscriber;
use Composer\InstalledVersions;

function fixtureCounter(): int
{
    static $value = 0;
    return ++$value;
}

final class RequestState
{
    public static int $value = 0;
}

$initialRequestEnvironment = getenv('BREF_FIXTURE_REQUEST_VALUE');
$identity = (string) ($_GET['identity'] ?? '');
putenv('BREF_FIXTURE_REQUEST_VALUE=' . $identity);
usleep(max(0, min(300, (int) ($_GET['sleep_ms'] ?? 0))) * 1000);
$hooks = is_file(CompatibilitySubscriber::hookFile())
    ? file(CompatibilitySubscriber::hookFile(), FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES)
    : [];
$contextNames = ['LAMBDA_INVOCATION_CONTEXT', 'LAMBDA_REQUEST_CONTEXT', '_X_AMZN_TRACE_ID'];
$contextPresence = [];
foreach ($contextNames as $name) {
    $contextPresence[$name] = array_key_exists($name, $_SERVER);
}

header('Content-Type: application/json');
header('Cache-Control: no-store');
echo json_encode([
    'ok' => true,
    'bref_version' => InstalledVersions::getPrettyVersion('bref/bref'),
    'bref_reference' => InstalledVersions::getReference('bref/bref'),
    'bref_class_path' => (new ReflectionClass(Bref::class))->getFileName(),
    'subscriber_registered' => Bref::events() instanceof \Bref\Listener\EventDispatcher,
    'runtime' => getenv('BREF_RUNTIME'),
    'task_root' => getenv('LAMBDA_TASK_ROOT'),
    'startup_count' => (int) getenv('BREF_FIXTURE_BEFORE_STARTUP'),
    'autoload_count' => (int) getenv('BREF_FIXTURE_AUTOLOAD_COUNT'),
    'startup_memory_transferred' => CompatibilitySubscriber::$memoryOnlyStartupFlag,
    'hook_events' => $hooks,
    'request_id' => $_SERVER['LAMBDA_REQUEST_ID'] ?? null,
    'identity' => $identity,
    'request_environment_before' => $initialRequestEnvironment,
    'request_environment_after' => getenv('BREF_FIXTURE_REQUEST_VALUE'),
    'function_static_counter' => fixtureCounter(),
    'class_static_counter' => ++RequestState::$value,
    'pid' => getmypid(),
    'method' => $_SERVER['REQUEST_METHOD'] ?? null,
    'raw_uri' => $_SERVER['REQUEST_URI'] ?? null,
    'body_sha256' => hash('sha256', file_get_contents('php://input')),
    'precision' => ini_get('precision'),
    'intl_available' => extension_loaded('intl'),
    'intl_result' => extension_loaded('intl') ? IntlChar::ord('€') : null,
    'ini_scanned' => php_ini_scanned_files(),
    'disable_functions' => explode(',', ini_get('disable_functions')),
    'post_max_size' => ini_get('post_max_size'),
    'upload_max_filesize' => ini_get('upload_max_filesize'),
    'configured_max_body_bytes' => (int) getenv('BREF_WEB_MAX_BODY_BYTES'),
    'event_context_presence' => $contextPresence,
], JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES);
