<?php declare(strict_types=1);

namespace BrefWebFixture;

use Bref\Context\Context;
use Bref\Event\Handler;
use Bref\Listener\BrefEventSubscriber;
use Psr\Http\Server\RequestHandlerInterface;

final class CompatibilitySubscriber extends BrefEventSubscriber
{
    // PHP objects/static state from the initialization helper are not transferred
    // to FPM. Only explicit environment/filesystem initialization is supported.
    public static bool $memoryOnlyStartupFlag = false;

    public static function hookFile(): string
    {
        return getenv('BREF_FIXTURE_HOOK_FILE') ?: '/tmp/bref-web/real-bref-hooks.log';
    }

    private static function record(string $event): void
    {
        if (file_put_contents(self::hookFile(), $event . "\n", FILE_APPEND | LOCK_EX) === false) {
            throw new \RuntimeException('Could not record fixture startup hook');
        }
    }

    public function beforeStartup(): void
    {
        self::$memoryOnlyStartupFlag = true;
        putenv('BREF_FIXTURE_BEFORE_STARTUP=' . ((int) getenv('BREF_FIXTURE_BEFORE_STARTUP') + 1));
        self::record('beforeStartup');
    }

    public function afterStartup(): void
    {
        self::record('afterStartup');
    }

    public function beforeInvoke(
        callable | Handler | RequestHandlerInterface $handler,
        mixed $event,
        Context $context,
    ): void {
        self::record('beforeInvoke');
    }

    public function afterInvoke(
        callable | Handler | RequestHandlerInterface $handler,
        mixed $event,
        Context $context,
        mixed $result,
        ?\Throwable $error = null,
    ): void {
        self::record('afterInvoke');
    }
}
