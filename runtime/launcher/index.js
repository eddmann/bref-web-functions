#!/usr/bin/env node
'use strict';

// Lambda's managed Node runtime only supervises processes. It serves no HTTP.
// PHP initialization prepares the environment before the selected gateway starts.
const { spawn } = require('node:child_process');
const { mkdir, writeFile } = require('node:fs/promises');
const net = require('node:net');
const path = require('node:path');
const { setTimeout: delay } = require('node:timers/promises');
const { tcpAddress, positiveInteger } = require('./config.cjs');

const STATE_DIR = '/tmp/bref-web';

function caddyConfig(env = process.env) {
  // Loaded only in reference mode: Rust ZIPs contain no Caddy configuration.
  return require(path.join(__dirname, '..', '..', 'reference', 'caddy', 'config.cjs')).caddyConfig(env);
}

async function main({ initializeRust = false } = {}) {
  const mode = process.env.BREF_WEB_GATEWAY || 'rust';
  if (mode !== 'rust' && mode !== 'caddy') throw new Error('BREF_WEB_GATEWAY must be rust or caddy');
  if (initializeRust && mode !== 'rust') throw new Error('The ZIP entrypoint supports the Rust gateway');
  if (process.argv.includes('--print-caddy-config')) {
    process.stdout.write(`${JSON.stringify(caddyConfig(), null, 2)}\n`);
    return;
  }

  const endpoint = tcpAddress(process.env.AWS_LAMBDA_HTTP_ENDPOINT || '0.0.0.0:8080', 'AWS_LAMBDA_HTTP_ENDPOINT');
  const startupMs = positiveInteger(process.env.BREF_WEB_STARTUP_TIMEOUT_MS, 8000, 'BREF_WEB_STARTUP_TIMEOUT_MS');
  const shutdownMs = positiveInteger(process.env.BREF_WEB_SHUTDOWN_TIMEOUT_MS, 900, 'BREF_WEB_SHUTDOWN_TIMEOUT_MS');
  const rustGraceMs = positiveInteger(process.env.BREF_WEB_SHUTDOWN_GRACE_MS, 500, 'BREF_WEB_SHUTDOWN_GRACE_MS', 0);
  if (mode === 'rust' && rustGraceMs + 300 > shutdownMs) {
    throw new Error('Node shutdown timeout must exceed Rust shutdown grace by at least 300ms');
  }
  const deadline = Date.now() + startupMs;
  const children = new Map();
  const lifecycle = new AbortController();
  let shuttingDown = false;
  let shutdownPromise;

  function signalChild(entry, signal) {
    if (!entry.child.pid) return;
    try {
      if (entry.processGroup) process.kill(-entry.child.pid, signal);
      else if (entry.child.exitCode === null && entry.child.signalCode === null) entry.child.kill(signal);
    } catch (error) {
      if (error.code !== 'ESRCH') console.error(`[launcher] could not send ${signal}: ${error.message}`);
    }
  }

  function shutdown(code, reason) {
    if (shutdownPromise) return shutdownPromise;
    shuttingDown = true;
    lifecycle.abort();
    console.error(`[launcher] stopping: ${reason}`);
    shutdownPromise = (async () => {
      const running = [...children.values()];
      for (const entry of running) signalChild(entry, entry.stopSignal);
      await Promise.race([
        Promise.all(running.map(({ closed }) => closed)),
        delay(shutdownMs),
      ]);
      // Keep the snapshot: the FPM master can exit before its worker group.
      for (const entry of running) signalChild(entry, 'SIGKILL');
      process.exit(code);
    })();
    return shutdownPromise;
  }

  function startChild(name, executable, args, stopSignal, env = process.env) {
    const processGroup = name === 'php-fpm';
    const child = spawn(executable, args, { env, stdio: 'inherit', detached: processGroup });
    let closedResolve;
    const closed = new Promise((resolve) => { closedResolve = resolve; });
    children.set(name, { child, stopSignal, closed, processGroup });
    child.once('error', (error) => {
      if (!shuttingDown) void shutdown(1, `${name} failed to start: ${error.message}`);
    });
    child.once('exit', (code, signal) => {
      if (!shuttingDown) void shutdown(1, `${name} exited unexpectedly (${signal || code})`);
    });
    child.once('close', () => {
      children.delete(name);
      closedResolve();
    });
    return child;
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
    if (mode === 'rust') {
      const rust = process.env.BREF_WEB_RUST_BIN || '/usr/local/bin/bref-web-gateway';
      // Lambda loads Node first. In a ZIP, this child performs shared PHP/Bref
      // initialization and then execs Rust, preserving its environment without
      // copying secrets through files, IPC or the parent Node process.
      const executable = initializeRust ? '/bin/bash' : rust;
      const args = initializeRust
        ? [path.join(process.env.LAMBDA_TASK_ROOT || '/var/task', 'compat', 'entrypoint.sh'), rust]
        : [];
      startChild('rust', executable, args, 'SIGTERM', {
        ...process.env,
        BREF_WEB_SHUTDOWN_GRACE_MS: String(rustGraceMs),
      });
    } else {
      const configPath = path.join(STATE_DIR, 'caddy.json');
      await mkdir(STATE_DIR, { recursive: true, mode: 0o700 });
      await writeFile(configPath, `${JSON.stringify(caddyConfig(), null, 2)}\n`, { mode: 0o600 });
      const fpmAddress = tcpAddress(process.env.BREF_WEB_FPM_ADDR || '127.0.0.1:9000', 'BREF_WEB_FPM_ADDR');
      startChild('php-fpm', process.env.BREF_WEB_FPM_BIN || '/opt/bin/php-fpm', [
        '-F', '-O', '-y', process.env.BREF_WEB_FPM_CONFIG || path.join(STATE_DIR, 'php-fpm.conf'),
        '-d', 'opcache.file_cache_only=0',
      ], 'SIGQUIT');
      await waitForTcp(fpmAddress, 'PHP-FPM');
      if (shuttingDown) return;
      startChild('caddy', process.env.BREF_WEB_CADDY_BIN || '/usr/local/bin/caddy', [
        'run', '--config', configPath,
      ], 'SIGTERM');
    }
    await waitForTcp(endpoint, mode === 'rust' ? 'Rust gateway' : 'Caddy');
    if (!shuttingDown) console.error(`[launcher] ${mode} ready on ${endpoint.listen}`);
  } catch (error) {
    if (!shuttingDown) await shutdown(1, error.message);
  }
}

module.exports = { main, caddyConfig };

if (require.main === module) {
  main().catch((error) => {
    console.error(`[launcher] ${error.message}`);
    process.exitCode = 1;
  });
}
