import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/** A packaged archive, verified against its CI checksum and manifest. */
export interface Archive {
  readonly path: string;
  readonly sha256: string;
  readonly architecture: string;
  readonly deploymentTarget: string;
}

/**
 * Load an archive produced by scripts/package.mjs, as uploaded by CI in
 * `aws-archives-<arch>`. Refuses a ZIP whose bytes differ from its `.sha256`
 * file, and (unless allowed) anything but an ARM64 `lambda-web` package:
 * Web Functions run only on ARM64.
 */
export function loadArchive(path: string, options: { readonly requireLambdaWeb: boolean }): Archive {
  const absolute = resolve(path);
  if (!existsSync(absolute)) {
    throw new Error(`Archive not found: ${absolute}. Download the CI artifact aws-archives-aarch64 into the archives directory.`);
  }
  const checksumFile = `${absolute}.sha256`;
  if (!existsSync(checksumFile)) {
    throw new Error(`Checksum file not found: ${checksumFile}`);
  }
  const sha256 = createHash('sha256').update(readFileSync(absolute)).digest('hex');
  const recorded = readFileSync(checksumFile, 'utf8').trim().split(/\s+/)[0];
  if (recorded !== sha256) {
    throw new Error(`${absolute} does not match ${checksumFile}: ${sha256} != ${recorded}`);
  }
  let manifest: { architecture?: string; deployment_target?: string };
  try {
    // The manifest lists every packaged file; a framework application's exceeds
    // execFileSync's default 1 MiB output buffer.
    manifest = JSON.parse(execFileSync('unzip', ['-p', absolute, 'package-manifest.json'], {
      encoding: 'utf8',
      maxBuffer: 256 * 1024 * 1024,
    }));
  } catch (error) {
    throw new Error(`Cannot read package-manifest.json from ${absolute} (needs unzip): ${(error as Error).message}`);
  }
  const architecture = manifest.architecture ?? 'unknown';
  const deploymentTarget = manifest.deployment_target ?? 'unknown';
  if (options.requireLambdaWeb && (architecture !== 'aarch64' || deploymentTarget !== 'lambda-web')) {
    throw new Error(`${absolute} is ${architecture}/${deploymentTarget}; Web Functions need an aarch64 lambda-web archive`);
  }
  return { path: absolute, sha256, architecture, deploymentTarget };
}
