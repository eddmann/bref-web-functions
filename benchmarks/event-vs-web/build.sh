#!/usr/bin/env bash
# Build the benchmark application from the tests/frameworks Laravel app.
#
#   benchmarks/event-vs-web/build.sh <laravel-app> <runtime-dir> <gateway> <output-dir>
#
# <laravel-app>  built by tests/frameworks/setup.sh laravel <dir>
# <runtime-dir>  and <gateway>: the CI archive's runtime/ and bin/bref-web-gateway
# Writes <output-dir>/app (the event function's code) and
# <output-dir>/archives/laravel-bench.zip(.sha256) (the Web Function archive).
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd -- "$here/../.." && pwd)"
laravel="$1" runtime="$2" gateway="$3" output="$4"
rm -rf "$output/app" && mkdir -p "$output/archives"
cp -R "$laravel" "$output/app"
cat "$here/routes.php" >> "$output/app/routes/web.php"
php -l "$output/app/routes/web.php" >/dev/null
node "$root/scripts/package.mjs" --runtime-dir "$runtime" --rust-binary "$gateway" \
    --architecture aarch64 --app-dir "$output/app" --output "$output/archives/laravel-bench.zip" >/dev/null
(cd "$output/archives" && shasum -a 256 laravel-bench.zip > laravel-bench.zip.sha256 && cat laravel-bench.zip.sha256)
