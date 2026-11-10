#!/usr/bin/env bash
# Build the Laravel and Symfony parity applications with their Bref bridges.
#
#   tests/frameworks/setup.sh laravel|symfony <empty-target-dir>
#
# Requires php, git and composer.phar (COMPOSER, default build/ci/composer.phar)
# on the host, and the bref-web-functions:local image for the Symfony cache
# warmup. --prefer-source clones locked commits with git where GitHub archive
# downloads are unavailable; Packagist metadata is still required.
set -euo pipefail
framework="$1"
target="$2"
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd -- "$here/../.." && pwd)"
composer=(php "${COMPOSER:-$root/build/ci/composer.phar}")
image="${BREF_WEB_IMAGE:-bref-web-functions:local}"
flags=(--prefer-source --no-interaction --no-progress)

case "$framework" in
    laravel)
        "${composer[@]}" create-project 'laravel/laravel:^13' "$target" --no-scripts "${flags[@]}"
        cd "$target"
        # bref/secrets-loader resolves bref-ssm: variables (exercised on AWS by infra/aws).
        "${composer[@]}" require bref/bref:^3 bref/laravel-bridge:^3 bref/secrets-loader:^1 --no-scripts -W "${flags[@]}"
        cp -R "$here/laravel/overlay/." .
        "${composer[@]}" install --no-dev --classmap-authoritative --no-scripts "${flags[@]}"
        # What `composer install` scripts do in a deployment build.
        php artisan package:discover --ansi
        ;;
    symfony)
        "${composer[@]}" create-project 'symfony/skeleton:7.4.*' "$target" "${flags[@]}"
        cd "$target"
        "${composer[@]}" require bref/bref:^3 bref/symfony-bridge:^3 -W "${flags[@]}"
        cp -R "$here/symfony/overlay/." .
        "${composer[@]}" dump-autoload --no-dev --classmap-authoritative
        # Bref's documented production recipe: disable Dotenv, configuration comes
        # from environment variables. Package it with --keep-dotenv.
        echo "<?php return [];" > .env.local.php
        rm -rf var/cache
        # Warm the production cache as a build machine would: not in Lambda mode.
        docker run --rm -v "$PWD:/var/task" -w /var/task -e APP_ENV=prod -e APP_DEBUG=0 -e APP_SECRET=build \
            -e DEFAULT_URI=https://example.com --entrypoint /bin/sh "$image" \
            -c 'unset LAMBDA_TASK_ROOT; /opt/bin/php bin/console cache:warmup'
        ;;
    *)
        printf 'usage: %s laravel|symfony <target>\n' "$0" >&2
        exit 2
        ;;
esac
chmod -R a+rX .
