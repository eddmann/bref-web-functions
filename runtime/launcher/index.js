#!/usr/bin/env node
'use strict';

// Lambda's managed Node runtime only supervises the Rust gateway. It serves no HTTP.
const { spawn } = require('node:child_process');
const net = require('node:net');
const path = require('node:path');
const { setTimeout: delay } = require('node:timers/promises');
const { tcpAddress, positiveInteger } = require('./config.cjs');

// initializeRust: run the shared PHP initialization, which execs Rust (the
// deployment archive). Without it, Rust starts directly (hardening tests).
async function main({ initializeRust = false } = {}) {
  // Measured from Node start: the closest local proxy for Lambda init time.
  const launched = Date.now() - Math.round(process.uptime() * 1000);
  const endpoint = tcpAddress(process.env.AWS_LAMBDA_HTTP_ENDPOINT || '0.0.0.0:8080', 'AWS_LAMBDA_HTTP_ENDPOINT');
  const startupMs = positiveInteger(process.env.BREF_WEB_STARTUP_TIMEOUT_MS, 8000, 'BREF_WEB_STARTUP_TIMEOUT_MS');
  const shutdownMs = positiveInteger(process.env.BREF_WEB_SHUTDOWN_TIMEOUT_MS, 900, 'BREF_WEB_SHUTDOWN_TIMEOUT_MS');
  const rustGraceMs = positiveInteger(process.env.BREF_WEB_SHUTDOWN_GRACE_MS, 500, 'BREF_WEB_SHUTDOWN_GRACE_MS', 0);
  if (rustGraceMs + 300 > shutdownMs) {
    throw new Error('Node shutdown timeout must exceed Rust shutdown grace by at least 300ms');
  }
  const rust = process.env.BREF_WEB_RUST_BIN;
  if (!rust) throw new Error('BREF_WEB_RUST_BIN must name the gateway binary');
  const deadline = Date.now() + startupMs;
  const lifecycle = new AbortController();
  let child;
  let closed;
  let shutdownPromise;

  function signalChild(signal) {
    if (!child?.pid || child.exitCode !== null || child.signalCode !== null) return;
    try {
      child.kill(signal);
    } catch (error) {
      if (error.code !== 'ESRCH') console.error(`[launcher] could not send ${signal}: ${error.message}`);
    }
  }

  function shutdown(code, reason) {
    if (shutdownPromise) return shutdownPromise;
    lifecycle.abort();
    // Lambda records stderr as ERROR: a requested stop is informational.
    (code === 0 ? console.log : console.error)(`[launcher] stopping: ${reason}`);
    shutdownPromise = (async () => {
      signalChild('SIGTERM');
      await Promise.race([closed, delay(shutdownMs)]);
      signalChild('SIGKILL');
      process.exit(code);
    })();
    return shutdownPromise;
  }

  async function waitForTcp(address, label) {
    const host = address.host === '0.0.0.0' ? '127.0.0.1' : address.host === '::' ? '::1' : address.host;
    while (!lifecycle.signal.aborted && Date.now() < deadline) {
      const listening = await new Promise((resolve) => {
        const socket = net.createConnection({ host, port: address.port });
        let settled = false;
        const finish = (ok) => {
          if (settled) return;
          settled = true;
          socket.destroy();
          lifecycle.signal.removeEventListener('abort', cancelled);
          resolve(ok);
        };
        const cancelled = () => finish(false);
        lifecycle.signal.addEventListener('abort', cancelled, { once: true });
        socket.once('connect', () => finish(true));
        socket.once('error', () => finish(false));
        socket.setTimeout(Math.min(200, Math.max(1, deadline - Date.now())), () => finish(false));
      });
      if (listening) return;
      if (!lifecycle.signal.aborted) await delay(25, undefined, { signal: lifecycle.signal }).catch(() => {});
    }
    throw new Error(`${label} did not listen within ${startupMs}ms`);
  }

  process.once('SIGTERM', () => { void shutdown(0, 'SIGTERM'); });
  process.once('SIGINT', () => { void shutdown(0, 'SIGINT'); });

  try {
    // Lambda loads Node first. In a ZIP, the child performs the shared PHP/Bref
    // initialization and then execs Rust, preserving its environment without
    // copying secrets through files, IPC or the parent Node process.
    // PHP needs the runtime's libraries to start; compat/bootstrap.php derives
    // everything else.
    const runtime = process.env.BREF_WEB_RUNTIME_ROOT;
    const libraries = [`${runtime}/lib`, `${runtime}/lib64`, process.env.LD_LIBRARY_PATH].filter(Boolean).join(':');
    child = initializeRust
      ? spawn(`${runtime}/bin/php`, ['-n', path.join(process.env.LAMBDA_TASK_ROOT, 'compat', 'bootstrap.php'), rust],
        { stdio: 'inherit', env: { ...process.env, LD_LIBRARY_PATH: libraries } })
      : spawn(rust, [], { stdio: 'inherit' });
    closed = new Promise((resolve) => { child.once('close', resolve); });
    child.once('error', (error) => { void shutdown(1, `rust failed to start: ${error.message}`); });
    child.once('exit', (code, signal) => { void shutdown(1, `rust exited unexpectedly (${signal || code})`); });
    await waitForTcp(endpoint, 'Rust gateway');
    if (!lifecycle.signal.aborted) console.log(`[launcher] ready on ${endpoint.listen} startup_ms=${Date.now() - launched}`);
  } catch (error) {
    if (!lifecycle.signal.aborted) await shutdown(1, error.message);
  }
}

module.exports = { main };

if (require.main === module) {
  main().catch((error) => {
    console.error(`[launcher] ${error.message}`);
    process.exitCode = 1;
  });
}
