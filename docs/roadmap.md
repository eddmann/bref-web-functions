# Roadmap

The primary implementation is Rust. Caddy remains a behavioral comparison and a
reference transport while the custom gateway is hardened. Small executable size
alone is not a reason to choose Rust for production.

## Runtime hardening

Controlled retirement on unconfirmed backend completion and connection-level
write cutoffs are implemented, with native-process regression coverage. Continue
hardening these boundaries:

1. Fuzz FastCGI framing and HTTP/CGI mappings; expand cancellation, truncation,
   overload and process-death tests.
2. Exercise long-running worker recycling, temporary-file cleanup, connection
   reuse and memory behavior with a real framework application.

## AWS and application validation

Native AMD64 and ARM64 CI now execute exact archives, including PHP extensions
and dynamic libraries. Preserve that coverage while completing these checks:

1. Deploy an identified archive to an isolated Lambda Web Function and verify
   startup, supplied listen address, request IDs, streaming, timeout/reset and
   shutdown behavior.
2. Verify credentials and refresh, SSM/Secrets Manager, IAM, VPC egress, logs and
   downstream service effects.
3. Run a real Laravel/Symfony application through sustained and burst traffic,
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
