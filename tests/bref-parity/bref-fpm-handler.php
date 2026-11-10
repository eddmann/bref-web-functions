<?php declare(strict_types=1);
// Drive Bref 3.0.15's real FpmHandler with API Gateway HTTP API (v2) events.
require '/var/task/vendor/autoload.php';
use Bref\Context\Context;
use Bref\Event\Http\HttpRequestEvent;
use Bref\FpmRuntime\FpmHandler;
$cases = json_decode(file_get_contents('/parity/cases.json'), true);
$fpm = new FpmHandler('/var/task/public/index.php');
$fpm->start();
$out = [];
foreach ($cases as $name => $c) {
    $headers = array_change_key_case($c['headers'], CASE_LOWER);
    $cookies = [];
    if (isset($headers['cookie'])) { $cookies = array_map('trim', explode(';', $headers['cookie'])); unset($headers['cookie']); }
    $event = ['version' => '2.0', 'routeKey' => '$default', 'rawPath' => $c['path'], 'rawQueryString' => $c['query'],
        'cookies' => $cookies, 'headers' => $headers, 'isBase64Encoded' => true, 'body' => base64_encode($c['body']),
        'requestContext' => ['requestId' => 'ctx-req', 'http' => ['method' => $c['method'], 'path' => $c['path'], 'protocol' => 'HTTP/1.1', 'sourceIp' => '203.0.113.7']]];
    $response = $fpm->handle($event, new Context('req-' . $name, (time() + 30) * 1000, 'arn:aws:lambda:eu-west-1:000000000000:function:x', 'Root=1-trace'));
    $out[$name] = ['status' => $response['statusCode'], 'headers' => $response['headers'], 'cookies' => $response['cookies'],
        'app' => json_decode($response['isBase64Encoded'] ? base64_decode($response['body']) : $response['body'], true)];
}
$fpm->stop();
echo json_encode($out, JSON_UNESCAPED_SLASHES);
