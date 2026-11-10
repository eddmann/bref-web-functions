# Running Bref on Lambda Web Functions?

Hey Matthieu,

Thanks for all the work you continue to do with the Bref project.

I've been exploring Lambda's new [Web Functions](https://docs.aws.amazon.com/lambda/latest/dg/web-functions.html)
and how Bref could run on them, and thought it might interest you. I'm not
proposing anything concrete yet; I'd mostly like to know whether there's any
appetite for this, maybe as something for the roadmap down the line.

## What it could mean for Bref

Possible gains:

- Apps that spend most of a request waiting on a database or API could get much
  cheaper and need far fewer environments, since several requests share one
  environment and waiting isn't billed.
- Real response streaming, responses over 6 MB, and the raw query string reaching
  PHP unchanged (#1503, #1661, #2159).
- No API Gateway in front (#1625).

Possible losses:

- Small or CPU-heavy pages cost more than on event functions.
- Node.js only for now, so PHP has to ship inside the ZIP with a small Node launcher,
  rather than the standard `provided.al2023` and layers setup. Maybe that changes
  as the service matures.
- Several PHP workers per environment surfaces things that can't happen with
  today's one-worker FPM runtime (see the Symfony note below).
- No `afterStartup`/invocation hooks or API Gateway context, as there is no
  invocation.

## The proof of concept

To see how this could work, I built a proof of concept that tries to be the
least invasive way to fit Bref: <https://github.com/eddmann/bref-web-functions>

It keeps as much of Bref as I could: Bref's own PHP build, PHP-FPM, the
`php.ini`/`php/conf.d` conventions, `beforeStartup`, `bref-ssm:` secrets and the
Laravel and Symfony bridges. Startup mirrors Bref's own layering: the Node
launcher (required by Web Functions) starts PHP, which validates settings,
writes the INI and FPM config, runs the secrets loader and `beforeStartup`, then
hands over to a gateway that forwards HTTP to a pool of PHP-FPM workers.

I first tried Caddy as that gateway, but it felt heavier than needed, and its
timeout and admission behaviour didn't map neatly onto what PHP-FPM needs. So I
ended up writing a small Rust HTTP-to-FastCGI gateway (a 1.1 MB static binary)
that admits a bounded number of requests, maps them to FastCGI the way Bref's
`FpmHandler` does, streams responses, enforces deadlines and retires the
environment if a PHP worker can't be confirmed stopped.

Rust is just one choice, though. The gateway could also be:

- **Node**: it's already running in every Web Function, and Bref already maintains
  JavaScript.
- **Caddy**, or another off-the-shelf server.
- **Rust**, as in the proof of concept.

## One thing I hit: a Symfony cache race

With several FPM workers starting at once, `BrefKernel::prepareCacheDir()` in the
Symfony bridge can race: one worker sees the cache directory another has just
created and fails with `symlink(): File exists` (7 of 192 cold-start requests
in a local test). It can't happen with today's one-worker FPM runtime, so it's
not a bug in Bref as it stands. Building the cache in a private directory and
`rename()`ing it into place fixed it (0 of 384); happy to open a PR if it's useful.

## What I tested

All on AWS (ARM64, a single Region, one client), so treat the numbers as
indicative rather than a full benchmark:

- Parity with Bref's own `FpmHandler`: every CGI variable PHP sees, plus
  Laravel 13 and Symfony 7.4 apps with their bridges. Only documented
  differences remain (the raw query string, and one Symfony content-type detail).
- Deployment, updates to a new revision with no failed requests, scaling,
  streaming, 64 MB responses, worker crashes, timeouts, retirement and
  replacement, `bref-ssm:` secrets, and memory with every worker busy.
- The same Laravel app as an event function (Bref FPM, 1,024 MB, Function URL)
  and as a Web Function (16 PHP-FPM workers), at AWS list prices:
  - Pages that wait 200 ms on I/O: roughly **3–5× cheaper** as a Web Function, and
    4 environments instead of 43 at 90 requests a second. Against a 512 MB event
    function I'd expect about 2× (estimated, not measured).
  - Small or CPU-heavy pages: roughly **2–3× more expensive**.
  - A burst of 40 cold requests: 4 cold starts instead of 40.
  - Warm latency was the same on both.
  - The event function returned 502 for responses over 6 MB, and Bref's FPM
    runtime delivered a streamed response all at once; the Web Function returned
    32 MB and streamed each chunk as it was written.

The method and raw results are in the
[benchmark](https://github.com/eddmann/bref-web-functions/tree/main/benchmarks/event-vs-web)
and [testing](https://github.com/eddmann/bref-web-functions/blob/main/docs/testing.md) docs.

## So

Is this something you'd be interested in for Bref at some point? If so, I'd love
your thoughts on the shape it should take (gateway choice, where it would live,
how the PHP build gets into the ZIP), and I'm happy to help however is useful.

Thanks again for Bref!
