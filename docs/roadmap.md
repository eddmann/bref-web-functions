# Roadmap

The primary implementation is Rust. Caddy remains a behavioral comparison and a
reference transport while the custom gateway is hardened. Small executable size
alone is not a reason to choose Rust for production.

## Runtime hardening

1. Add controlled retirement/recovery when backend termination cannot be
   confirmed. Current behavior closes admission until an external restart.
2. Enforce hard deadlines for stalled downstream socket writes, beyond response
   body polling timers.
3. Fuzz FastCGI framing and HTTP/CGI mappings; expand cancellation, truncation,
   overload and process-death tests.
4. Exercise long-running worker recycling, temporary-file cleanup, connection
   reuse and memory behavior with a real framework application.

## Native ARM64 and AWS validation

1. Execute the exact ARM64 archive natively, including PHP extensions and dynamic
   library loading. Compilation/ELF inspection is not execution proof.
2. Deploy that identified archive to an isolated Lambda Web Function and verify
   startup, supplied listen address, request IDs, streaming, timeout/reset and
   shutdown behavior.
3. Verify credentials and refresh, SSM/Secrets Manager, IAM, VPC egress, logs and
   downstream service effects.
4. Run a real Laravel/Symfony application through sustained and burst traffic,
   collect actual CPU/memory/cost evidence, and compare it with the existing Bref
   event deployment as well as Caddy.

## Compatibility and release criteria

Decide which missing Bref hooks/context have meaningful equivalents in the web
model. Implement supported equivalents explicitly and document the remainder;
do not synthesize API Gateway or invocation metadata that AWS did not supply.

Before a production release, require reproducible artifacts, a stable documented
configuration contract, verified recovery/rollback behavior, real application
coverage and evidence that the custom transport is worth maintaining. See
[verification](verification.md) for the checks already recorded.
