# bref/symfony-bridge: prepare the cache directory atomically

[`atomic-cache-dir.patch`](atomic-cache-dir.patch) applies to
[brefphp/symfony-bridge](https://github.com/brefphp/symfony-bridge) at `3c6a789`
(`src/BrefKernel.php`).

**Problem.** On its first request each PHP-FPM worker runs
`BrefKernel::prepareCacheDir()`, which returns early if `/tmp/cache/<env>` exists
and otherwise creates it, then copies and symlinks the read-only cache into it.
With several workers starting together, one worker sees the directory another has
only just created and either boots from a half-filled cache or fails:

```text
Uncaught Exception: Failed to create "symbolic" link from
"/var/task/var/cache/prod/Container…" to "/tmp/cache/prod/Container…": symlink(): File exists
```

Bref's FPM runtime runs one worker, so this never shows there. It does with any
multi-worker FPM, such as Lambda Web Functions.

**Fix.** Each worker builds the cache in its own directory, then `rename()`s it
into place. Exactly one rename succeeds; the others discard their copy.

**Evidence** (`tests/frameworks/cold_start.py`, Symfony 7.4.23, 16 workers, 32
simultaneous first requests per fresh environment):

| | Cold starts | First requests failed | Times the cache was prepared |
| --- | --- | --- | --- |
| Unpatched | 6 | 7 of 192 (500) | 5–14 per start |
| Patched | 12 | 0 of 384 | 1 per start, no leftover directories |
