<?php declare(strict_types=1);

use Bref\Bref;
use BrefWebFixture\CompatibilitySubscriber;

// Composer loads this in the initialization helper and in each FPM request.
// The request-local putenv change must be reset by normal PHP request cleanup.
putenv('BREF_FIXTURE_AUTOLOAD_COUNT=' . ((int) getenv('BREF_FIXTURE_AUTOLOAD_COUNT') + 1));
Bref::events()->subscribe(new CompatibilitySubscriber());
