'use strict';

// Dynamic import is valid in both CommonJS and ES modules. PHP applications
// often include package.json with type=module for asset tooling; leave it intact.
import('./launcher/entry.cjs').catch((error) => {
  console.error(`[launcher] ${error.message}`);
  process.exitCode = 1;
});
