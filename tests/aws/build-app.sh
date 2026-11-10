#!/usr/bin/env bash
# Stage the AWS test-only application: the test routes plus the deployable
# diagnostic page as their fallback. Package the result as a test-only archive.
#
#   tests/aws/build-app.sh <empty-target-dir>
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd -- "$here/../.." && pwd)"
target="$1"
mkdir -p "$target/public"
if [[ -n "$(ls -A "$target/public")" ]]; then
    printf 'target public/ must be empty: %s\n' "$target" >&2
    exit 2
fi
cp "$root/examples/diagnostic/public/index.php" "$target/public/diagnostic.php"
cp "$here/public/index.php" "$target/public/index.php"
chmod -R a+rX "$target"
