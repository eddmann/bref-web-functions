#!/usr/bin/env node
import { App } from 'aws-cdk-lib';
import { RetentionDays } from 'aws-cdk-lib/aws-logs';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { loadArchive } from '../lib/archives';
import { EventFunctionApp, ExplorationStack, WebFunctionApp } from '../lib/exploration-stack';

// Settings come from CDK context: `cdk deploy -c name=value`. See README.md.
const app = new App();
const context = (name: string, fallback?: string): string => {
  const value = app.node.tryGetContext(name);
  if (value === undefined || value === null || value === '') {
    if (fallback === undefined) {
      throw new Error(`Missing context value: -c ${name}=...`);
    }
    return fallback;
  }
  return String(value);
};
const integer = (name: string, fallback: number): number => {
  const value = Number(context(name, String(fallback)));
  if (!Number.isInteger(value)) {
    throw new Error(`Context ${name} must be an integer`);
  }
  return value;
};

const archives = context('archives', join(__dirname, '..', 'archives'));
const requireLambdaWeb = context('allowNonArm64Archives', 'false') !== 'true';
const selected = context('apps', 'diagnostic,test').split(',').map((name) => name.trim()).filter(Boolean);

const testToken = (): string => {
  const token = context('testToken');
  if (!/^[A-Za-z0-9_-]{16,128}$/.test(token)) {
    throw new Error('testToken must be 16-128 letters, digits, "-" or "_"');
  }
  return token;
};
// Framework secrets for a public test endpoint: derived from the test token,
// so they are neither committed nor the parity suite's well-known values.
const derived = (purpose: string): Buffer => createHash('sha256').update(`${purpose}:${testToken()}`).digest();

const laravelEnvironment = (): Record<string, string> => ({
  APP_ENV: 'production', APP_DEBUG: 'false', APP_KEY: `base64:${derived('laravel').toString('base64')}`,
  APP_URL: 'https://example.com', LOG_CHANNEL: 'stderr',
  SESSION_DRIVER: 'file', CACHE_STORE: 'array', DB_CONNECTION: 'sqlite',
});

// benchmarks/event-vs-web: one Laravel application as Web Functions at several
// concurrency levels and as a Bref event function, for comparison.
const benchmark = selected.includes('bench');
const benchConcurrency = context('benchConcurrency', '4,16,32,64').split(',').map(Number);
const benchApps: WebFunctionApp[] = benchmark
  ? benchConcurrency.map((maxInFlight) => ({
    id: `bench-c${maxInFlight}`,
    archive: loadArchive(join(archives, 'laravel-bench.zip'), { requireLambdaWeb }),
    environment: laravelEnvironment(),
    maxInFlight,
  }))
  : [];
const eventApps: EventFunctionApp[] = benchmark
  ? [{
    id: 'bench-event',
    codeDirectory: context('benchAppDirectory'),
    // Bref 3.0.15's arm-php-84 layer (vendor/bref/bref/layers.json).
    layerArn: context('brefLayerArn', `arn:aws:lambda:${process.env.CDK_DEFAULT_REGION}:873528684822:layer:arm-php-84:25`),
    memorySize: integer('benchEventMemory', 1024),
    environment: laravelEnvironment(),
  }]
  : [];

const apps: WebFunctionApp[] = selected.filter((name) => name !== 'bench').map((name): WebFunctionApp => {
  switch (name) {
    case 'diagnostic':
      return { id: 'diagnostic', archive: loadArchive(join(archives, 'diagnostic.zip'), { requireLambdaWeb }) };
    case 'test':
      // Guards the test-only routes on a public endpoint (tests/aws/public/index.php).
      return {
        id: 'test',
        archive: loadArchive(join(archives, 'aws-test-only.zip'), { requireLambdaWeb }),
        environment: { AWS_TEST_TOKEN: testToken() },
      };
    // Case 14: the tests/frameworks applications, packaged locally (Symfony with
    // --keep-dotenv) with the CI runtime. Same settings as tests/frameworks/run.py.
    case 'laravel':
      return {
        id: 'laravel',
        archive: loadArchive(join(archives, 'laravel.zip'), { requireLambdaWeb }),
        environment: laravelEnvironment(),
        ssmProbeValue: 'resolved-by-bref-ssm',
      };
    case 'symfony':
      return {
        id: 'symfony',
        archive: loadArchive(join(archives, 'symfony.zip'), { requireLambdaWeb }),
        environment: {
          APP_ENV: 'prod', APP_DEBUG: '0', APP_SECRET: derived('symfony').toString('hex'),
          DEFAULT_URI: 'https://example.com', APP_SHARE_DIR: '/tmp/share',
        },
      };
    default:
      throw new Error(`Unknown app "${name}"; choose from diagnostic,test,laravel,symfony,bench`);
  }
});

// Unset: the runtime default (passthrough). `ingress` when nothing sits in front of the endpoint.
function forwardedHeaders(): 'passthrough' | 'ingress' | undefined {
  const mode = app.node.tryGetContext('forwardedHeaders');
  if (mode === undefined || mode === null || mode === '') {
    return undefined;
  }
  if (mode !== 'passthrough' && mode !== 'ingress') {
    throw new Error('forwardedHeaders must be passthrough or ingress');
  }
  return mode;
}

const retentionDays = integer('logRetentionDays', 7);
const retention = (Object.values(RetentionDays) as number[]).includes(retentionDays)
  ? (retentionDays as RetentionDays)
  : undefined;
if (retention === undefined) {
  throw new Error(`logRetentionDays ${retentionDays} is not a CloudWatch retention value`);
}

new ExplorationStack(app, context('stackName', 'BrefWebExploration'), {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION },
  description: 'bref-web-functions AWS exploration: tested archives as Lambda Web Functions',
  apps: [...apps, ...benchApps],
  eventApps,
  namePrefix: context('namePrefix', 'bref-web'),
  maxInFlight: integer('maxInFlight', 12),
  timeoutSeconds: integer('timeoutSeconds', 30),
  maxEnvironments: integer('maxEnvironments', 10),
  rateLimit: integer('rateLimit', 100),
  logRetention: retention,
  forwardedHeaders: forwardedHeaders(),
});
