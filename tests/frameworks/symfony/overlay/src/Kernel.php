<?php

namespace App;

use Bref\SymfonyBridge\BrefKernel;
use Symfony\Bundle\FrameworkBundle\Kernel\MicroKernelTrait;

// As documented by Bref: the kernel extends BrefKernel so the cache and logs
// move to /tmp when LAMBDA_TASK_ROOT is set.
class Kernel extends BrefKernel
{
    use MicroKernelTrait;
}
