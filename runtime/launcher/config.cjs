'use strict';

const net = require('node:net');

function tcpAddress(value, name) {
  const separator = value.lastIndexOf(':');
  const host = value.slice(0, separator).replace(/^\[|\]$/g, '');
  const portText = value.slice(separator + 1);
  const port = Number(portText);
  if (separator < 1 || !host || !/^\d+$/.test(portText) || port < 1 || port > 65535) {
    throw new Error(`${name} must be a host:port address`);
  }
  return { host, port, listen: net.isIP(host) === 6 ? `[${host}]:${port}` : `${host}:${port}` };
}

function positiveInteger(value, fallback, name, minimum = 1) {
  if (value === undefined) return fallback;
  if (!/^\d+$/.test(value) || Number(value) < minimum || !Number.isSafeInteger(Number(value))) {
    throw new Error(`${name} must be an integer >= ${minimum}`);
  }
  return Number(value);
}

module.exports = { tcpAddress, positiveInteger };
