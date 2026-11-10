
// --- benchmarks/event-vs-web: appended to the Laravel test application ------
// The same routes run as a Bref event function and as a Web Function.

// One random ID per execution environment, shared by its PHP workers, so both
// runtimes report which environment served a request in the same way.
if (! function_exists('bench_environment')) {
function bench_environment(): string
{
    $file = '/tmp/bench-environment-id';
    if (($handle = @fopen($file, 'x')) !== false) {
        fwrite($handle, bin2hex(random_bytes(8)));
        fclose($handle);
    }
    return trim((string) @file_get_contents($file)) ?: 'unknown';
}

function bench_response(array $data): \Illuminate\Http\JsonResponse
{
    return response()->json($data + ['environment' => bench_environment(), 'pid' => getmypid()]);
}
}

// A typical small page: framework boot, routing, a session, JSON.
Route::get('/bench/fast', fn () => bench_response(['ok' => true]));

// Waiting on I/O (a database or API call), without using CPU.
Route::get('/bench/io', function (Request $request) {
    $milliseconds = max(0, min((int) $request->query('ms', 200), 5000));
    usleep($milliseconds * 1000);
    return bench_response(['waited_ms' => $milliseconds]);
});

// CPU-bound work for about the given wall time.
Route::get('/bench/cpu', function (Request $request) {
    $until = hrtime(true) + max(0, min((int) $request->query('ms', 50), 2000)) * 1_000_000;
    $value = 0;
    while (hrtime(true) < $until) {
        $value = crc32((string) $value);
    }
    return bench_response(['value' => $value]);
});

// Limits: response size and streaming.
Route::get('/bench/large', fn (Request $request) => response(str_repeat('x', max(1, min((int) $request->query('mb', 1), 64)) * 1048576))
    ->header('Content-Type', 'application/octet-stream'));

Route::get('/bench/stream', fn () => response()->stream(function (): void {
    for ($chunk = 1; $chunk <= 5; $chunk++) {
        printf("chunk=%d at=%.3f\n", $chunk, microtime(true));
        flush();
        if ($chunk < 5) {
            usleep(1_000_000);
        }
    }
}, 200, ['Content-Type' => 'text/plain', 'Cache-Control' => 'no-store']));
