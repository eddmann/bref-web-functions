'use strict';

// Managed Node remains the supervisor. The initialization child execs Rust in
// place, so PHP secrets never need to enter the parent Node process.
const path = require('node:path');
const root = path.dirname(__dirname);
const definition = require('../runtime-config.json');
process.env.LAMBDA_TASK_ROOT = root;
process.env.BREF_WEB_RUNTIME_ROOT = path.join(root, 'runtime');
process.env.BREF_WEB_RUST_BIN = path.join(root, 'bin', 'bref-web-gateway');
process.env.BREF_WEB_SCRIPT_FILENAME ||= path.join(root, definition.front_controller);
process.env.BREF_WEB_DOCUMENT_ROOT ||= path.join(root, definition.document_root);

require('./index.cjs').main({ initializeRust: true }).catch((error) => {
  console.error(`[launcher] ${error.message}`);
  process.exitCode = 1;
});
