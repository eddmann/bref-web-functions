'use strict';

// Caddy is the reference transport; shared supervision lives under runtime/.
const path = require('node:path');
const { tcpAddress, positiveInteger } = require('../../runtime/launcher/config.cjs');

const MAX_REQUEST_BYTES = 6 * 1024 * 1024;
const HEALTH_PATH = '/__bref_web/health';
// Pinned Caddy 2.11.9 diagnostics. A changed upstream message fails closed:
// the original internal status remains instead of rewriting another error.
const FCGI_UNKNOWN_LENGTH = 'request body has no known length; increase request_buffers so the body can be buffered in full to determine it';
const BODY_LIMIT_ERROR = 'request body: http: request body too large';

function bodyLimitErrorRoute() {
  return {
    routes: [{
      match: [{ vars: {
        '{http.error.status_code}': ['411'],
        '{http.error.message}': [FCGI_UNKNOWN_LENGTH],
      } }],
      handle: [
        // Only on Caddy's internal FastCGI length error: the original body's
        // MaxBytesReader retains its size-limit error after proxy buffering.
        // Its placeholder returns RequestBodyLimitError, never body text, when
        // that actual limit was exceeded. Successful requests do no extra read.
        { handler: 'vars', bref_body_read_error: '{http.request.body}' },
        { handler: 'subroute', routes: [{
          match: [{ vars: { bref_body_read_error: [BODY_LIMIT_ERROR] } }],
          handle: [{
            handler: 'static_response',
            status_code: 413,
            body: 'Request body exceeds the configured limit.\n',
          }],
          terminal: true,
        }] },
      ],
      // Caddy's errorEmptyHandler preserves the original internal status if
      // the limit marker does not match. PHP HTTP responses never enter this
      // error chain, so a genuine application 411 passes through unchanged.
    }],
  };
}

function caddyConfig(env = process.env) {
  const endpoint = tcpAddress(env.AWS_LAMBDA_HTTP_ENDPOINT || '0.0.0.0:8080', 'AWS_LAMBDA_HTTP_ENDPOINT');
  const fpm = tcpAddress(env.BREF_WEB_FPM_ADDR || '127.0.0.1:9000', 'BREF_WEB_FPM_ADDR');
  const script = env.BREF_WEB_SCRIPT_FILENAME || '/var/task/public/index.php';
  const root = env.BREF_WEB_DOCUMENT_ROOT || '/var/task/public';
  const workers = positiveInteger(env.BREF_WEB_MAX_IN_FLIGHT, 4, 'BREF_WEB_MAX_IN_FLIGHT');
  const queue = positiveInteger(env.BREF_WEB_QUEUE_CAPACITY, 4, 'BREF_WEB_QUEUE_CAPACITY', 0);
  const maxBody = positiveInteger(env.BREF_WEB_MAX_BODY_BYTES, MAX_REQUEST_BYTES, 'BREF_WEB_MAX_BODY_BYTES');
  const requestMs = positiveInteger(env.BREF_WEB_REQUEST_TIMEOUT_MS, 30000, 'BREF_WEB_REQUEST_TIMEOUT_MS');
  if (!Number.isSafeInteger(workers + queue) || !Number.isSafeInteger(requestMs * 1000000)) {
    throw new Error('The concurrency or timeout configuration is too large');
  }
  if (!path.isAbsolute(script) || !path.isAbsolute(root)) {
    throw new Error('The front controller and document root must be absolute paths');
  }

  return {
    admin: { disabled: true, config: { persist: false } },
    apps: {
      http: {
        servers: {
          php: {
            listen: [endpoint.listen],
            protocols: ['h1'],
            automatic_https: { disable: true },
            errors: bodyLimitErrorRoute(),
            routes: [
              { handle: [{ handler: 'request_body', max_size: maxBody }] },
              {
                match: [{ path: [HEALTH_PATH], method: ['GET'] }],
                handle: [{
                  handler: 'static_response',
                  status_code: 200,
                  headers: { 'Content-Type': ['application/json'] },
                  body: '{"ok":true,"gateway":"caddy"}\n',
                }],
                terminal: true,
              },
              // The UUID is only a local fallback: AWS supplies and sanitizes its
              // lambda-web-request-id header before forwarding real requests.
              { handle: [{ handler: 'vars', lambda_request_id: '{http.request.uuid}' }] },
              {
                match: [{ header: { 'Lambda-Web-Request-Id': ['*'] } }],
                handle: [{ handler: 'vars', lambda_request_id: '{http.request.header.Lambda-Web-Request-Id}' }],
              },
              {
                handle: [
                  // Caddy keeps OriginalRequestCtxKey before rewriting. Its
                  // FastCGI transport uses that original URI for REQUEST_URI;
                  // rewriting only the path also preserves the raw query string.
                  { handler: 'rewrite', uri: '/index.php' },
                  {
                    handler: 'reverse_proxy',
                    // Bounds upstream requests, but deliberately does not claim
                    // to reproduce Rust's semaphore + timed queue policy.
                    upstreams: [{ dial: fpm.listen, max_requests: workers + queue }],
                    // One extra byte lets CopyN observe EOF for an exactly-at-
                    // limit chunked body. request_body still enforces maxBody.
                    request_buffers: maxBody + 1,
                    // Positive flushing preserves upstream cancellation on a
                    // client disconnect; -1 intentionally changes that behavior.
                    flush_interval: 10 * 1000 * 1000,
                    transport: {
                      protocol: 'fastcgi',
                      root,
                      split_path: ['.php'],
                      capture_stderr: true,
                      read_timeout: requestMs * 1000000,
                      write_timeout: requestMs * 1000000,
                      env: {
                        SCRIPT_FILENAME: script,
                        SCRIPT_NAME: '/index.php',
                        DOCUMENT_ROOT: root,
                        PATH_INFO: '{http.request.orig_uri.path}',
                        // Lambda terminates HTTPS before this plain-HTTP hop.
                        // Do not trust a caller-supplied forwarded scheme.
                        HTTPS: 'on',
                        REQUEST_SCHEME: 'https',
                        SERVER_PORT: '443',
                        LAMBDA_REQUEST_ID: '{http.vars.lambda_request_id}',
                      },
                    },
                  },
                ],
              },
            ],
          },
        },
      },
    },
  };
}

module.exports = { caddyConfig };
