<?php

use Illuminate\Foundation\Http\Middleware\PreventRequestForgery;
use Illuminate\Foundation\Http\Middleware\ValidateCsrfToken;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Route;

// Laravel 13 renamed the CSRF middleware; the probe accepts unauthenticated POSTs.
$csrf = class_exists(PreventRequestForgery::class) ? PreventRequestForgery::class : ValidateCsrfToken::class;

// Reports what the framework derives from the request, for runtime comparison.
Route::match(['GET', 'POST'], '/probe/{rest}', function (Request $request, string $rest) {
    return response()->json([
        'framework' => 'laravel ' . app()->version(),
        'method' => $request->method(),
        'rest' => $rest,
        'client_ip' => $request->ip(),
        'client_ips' => $request->ips(),
        'secure' => $request->secure(),
        'scheme' => $request->getScheme(),
        'host' => $request->getHost(),
        'port' => $request->getPort(),
        'root' => $request->root(),
        'url' => $request->url(),
        'full_url' => $request->fullUrl(),
        'path' => $request->path(),
        'request_uri' => $request->getRequestUri(),
        'route_url' => route('probe', ['rest' => 'x/y']),
        'url_helper' => url('/elsewhere'),
        'asset' => asset('app.css'),
        'query' => $request->query(),
        'post' => $request->post(),
        'json' => $request->isJson() ? $request->json()->all() : null,
        'files' => array_map(fn ($file) => $file->getSize(), $request->allFiles()),
        'user' => $request->getUser(),
        'environment' => app()->environment(),
        'storage_path' => storage_path(),
        'config_cache' => app()->getCachedConfigPath(),
        'services_cache' => app()->getCachedServicesPath(),
        'session_driver' => config('session.driver'),
        'session_secure' => config('session.secure'),
        'log_default' => config('logging.default'),
        'cache_default' => config('cache.default'),
        'view_compiled' => config('view.compiled'),
        // Set on AWS (infra/aws) to bref-ssm:<parameter>; Bref's secrets loader resolves it.
        'ssm_probe' => getenv('PROBE_SSM') ?: null,
    ]);
})->where('rest', '.*')->name('probe')->withoutMiddleware($csrf);

Route::get('/session', function (Request $request) {
    $request->session()->put('count', $request->session()->get('count', 0) + 1);
    return response()->json(['count' => $request->session()->get('count')]);
});

Route::get('/redirect', fn () => redirect()->route('probe', ['rest' => 'after']));

Route::get('/error', function () {
    throw new RuntimeException('Deliberate framework parity error');
});

Route::get('/stream', fn () => response()->stream(function (): void {
    foreach (['one', 'two', 'three'] as $part) {
        echo $part, "\n";
        flush();
    }
}));
