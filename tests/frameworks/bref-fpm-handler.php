<?php declare(strict_types=1);

// Drive the application's own Bref FpmHandler with API Gateway HTTP API (v2)
// events, as Bref's FPM runtime does on Lambda. Cases sharing a "jar" carry
// response cookies forward, like a browser.
require getenv('LAMBDA_TASK_ROOT') . '/vendor/autoload.php';

use Bref\Bref;
use Bref\Context\Context;
use Bref\FpmRuntime\FpmHandler;
use Bref\LazySecretsLoader;
use Bref\Runtime\ColdStartTracker;

$cases = json_decode(file_get_contents($argv[1]), true, flags: JSON_THROW_ON_ERROR);
// The startup sequence of Bref\FpmRuntime\Main::run(), without the Lambda
// runtime API: bridges rely on beforeStartup (the Laravel bridge caches its
// configuration there and exports APP_CONFIG_CACHE to the FPM workers).
ColdStartTracker::init();
LazySecretsLoader::loadSecretEnvironmentVariables();
Bref::events()->beforeStartup();
$fpm = new FpmHandler(getenv('LAMBDA_TASK_ROOT') . '/public/index.php');
$fpm->start();
Bref::events()->afterStartup();
ColdStartTracker::coldStartFinished();
$jars = [];
$out = [];
foreach ($cases as $case) {
    $headers = array_change_key_case($case['headers'], CASE_LOWER);
    $cookies = [];
    if (isset($headers['cookie'])) {
        $cookies = array_map('trim', explode(';', $headers['cookie']));
        unset($headers['cookie']);
    }
    foreach ($jars[$case['jar'] ?? ''] ?? [] as $name => $value) {
        $cookies[] = "$name=$value";
    }
    $event = [
        'version' => '2.0', 'routeKey' => '$default', 'rawPath' => $case['path'],
        'rawQueryString' => $case['query'], 'cookies' => $cookies, 'headers' => $headers,
        'isBase64Encoded' => true, 'body' => $case['body_base64'],
        'requestContext' => ['requestId' => 'ctx-' . $case['name'], 'http' => [
            'method' => $case['method'], 'path' => $case['path'], 'protocol' => 'HTTP/1.1',
            'sourceIp' => '203.0.113.7',
        ]],
    ];
    $context = new Context('req-' . $case['name'], (time() + 30) * 1000, 'arn:aws:lambda:eu-west-1:000000000000:function:parity', 'Root=1-parity');
    $response = $fpm->handle($event, $context);
    if (isset($case['jar'])) {
        foreach ($response['cookies'] as $cookie) {
            [$pair] = explode(';', $cookie, 2);
            [$name, $value] = explode('=', $pair, 2) + [1 => ''];
            $jars[$case['jar']][trim($name)] = trim($value);
        }
    }
    $body = $response['isBase64Encoded'] ? base64_decode($response['body']) : $response['body'];
    $out[$case['name']] = [
        'status' => $response['statusCode'],
        'headers' => array_change_key_case((array) $response['headers'], CASE_LOWER),
        'set_cookie' => $response['cookies'],
        'body_base64' => base64_encode($body),
    ];
}
$fpm->stop();
echo json_encode($out, JSON_UNESCAPED_SLASHES);
