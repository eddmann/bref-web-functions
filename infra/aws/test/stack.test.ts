import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { RetentionDays } from 'aws-cdk-lib/aws-logs';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { Archive, loadArchive } from '../lib/archives';
import { ExplorationStack, validateSettings } from '../lib/exploration-stack';

/** A minimal ZIP with the packager's manifest and a matching .sha256 file. */
function fakeArchive(name: string, architecture: string, target: string, files: string[] = []): string {
  const directory = mkdtempSync(join(tmpdir(), 'bref-web-cdk-'));
  writeFileSync(join(directory, 'package-manifest.json'), JSON.stringify({ architecture, deployment_target: target, files }));
  writeFileSync(join(directory, 'index.js'), '');
  const zip = join(directory, name);
  execFileSync('zip', ['-q', '-X', zip, 'package-manifest.json', 'index.js'], { cwd: directory });
  const sha256 = createHash('sha256').update(readFileSync(zip)).digest('hex');
  writeFileSync(`${zip}.sha256`, `${sha256}  ${name}\n`);
  return zip;
}

test('loadArchive accepts a matching aarch64 lambda-web archive', () => {
  const archive = loadArchive(fakeArchive('diagnostic.zip', 'aarch64', 'lambda-web'), { requireLambdaWeb: true });
  assert.equal(archive.architecture, 'aarch64');
  assert.match(archive.sha256, /^[0-9a-f]{64}$/);
});

test('loadArchive reads a framework-sized manifest over 1 MiB', () => {
  const files = Array.from({ length: 20000 }, (_, index) => `vendor/package/src/File${index}.php`.padEnd(80, '_'));
  const archive = loadArchive(fakeArchive('laravel.zip', 'aarch64', 'lambda-web', files), { requireLambdaWeb: true });
  assert.equal(archive.deploymentTarget, 'lambda-web');
});

test('loadArchive rejects changed bytes and non-ARM64 archives', () => {
  const tampered = fakeArchive('diagnostic.zip', 'aarch64', 'lambda-web');
  writeFileSync(`${tampered}.sha256`, `${'0'.repeat(64)}  diagnostic.zip\n`);
  assert.throws(() => loadArchive(tampered, { requireLambdaWeb: true }), /does not match/);
  const local = fakeArchive('diagnostic.zip', 'x86_64', 'local-test-only');
  assert.throws(() => loadArchive(local, { requireLambdaWeb: true }), /need an aarch64 lambda-web archive/);
  assert.equal(loadArchive(local, { requireLambdaWeb: false }).architecture, 'x86_64');
});

test('validateSettings enforces Web Function and runtime limits', () => {
  const valid = { maxInFlight: 4, timeoutSeconds: 30, maxEnvironments: 10, rateLimit: 100 };
  validateSettings(valid);
  assert.throws(() => validateSettings({ ...valid, maxInFlight: 129 }));
  assert.throws(() => validateSettings({ ...valid, timeoutSeconds: 4 }));
  assert.throws(() => validateSettings({ ...valid, maxEnvironments: 1 }));
  assert.throws(() => validateSettings({ ...valid, rateLimit: 0 }));
  assert.throws(() => validateSettings({ ...valid, rateLimit: 1500 }));
  validateSettings({ ...valid, rateLimit: 2000 });
});

test('stack pins each archive version and matches concurrency to PHP workers', () => {
  const archive = (name: string): Archive => loadArchive(fakeArchive(name, 'aarch64', 'lambda-web'), { requireLambdaWeb: true });
  const stack = new ExplorationStack(new App(), 'Test', {
    apps: [
      { id: 'diagnostic', archive: archive('diagnostic.zip') },
      { id: 'test', archive: archive('aws-test-only.zip'), environment: { AWS_TEST_TOKEN: 'token-0123456789abcdef' } },
    ],
    namePrefix: 'bref-web',
    maxInFlight: 6,
    timeoutSeconds: 40,
    maxEnvironments: 10,
    rateLimit: 200,
    logRetention: RetentionDays.ONE_WEEK,
  });
  const template = Template.fromStack(stack);

  template.hasResourceProperties('AWS::S3::Bucket', { VersioningConfiguration: { Status: 'Enabled' } });
  template.hasResourceProperties('AWS::S3::BucketPolicy', {
    PolicyDocument: { Statement: Match.arrayWith([Match.objectLike({
      Principal: { Service: 'lambda.amazonaws.com' },
      Action: ['s3:GetObject', 's3:GetObjectVersion'],
    })]) },
  });
  template.resourceCountIs('AWS::Lambda::WebFunction', 2);
  template.hasResourceProperties('AWS::Lambda::WebFunction', { FunctionName: 'bref-web-test' });

  template.hasResourceProperties('AWS::Lambda::WebFunctionRevision', {
    FunctionName: 'bref-web-test',
    BuildConfig: {
      RuntimeConfig: { Runtime: 'nodejs24.x' },
      CodeConfig: { S3Object: { Key: Match.anyValue(), VersionId: Match.anyValue() } },
    },
    ServiceConfig: {
      MaxConcurrencyPerEnvironment: 6,
      TimeoutSeconds: 40,
      EnvironmentVariables: {
        BREF_WEB_MAX_IN_FLIGHT: '6',
        BREF_WEB_REQUEST_TIMEOUT_MS: '40000',
        BREF_WEB_FPM_TIMEOUT_SECONDS: '38',
        AWS_TEST_TOKEN: 'token-0123456789abcdef',
      },
    },
  });
  const revisions = template.findResources('AWS::Lambda::WebFunctionRevision');
  const diagnostic = Object.values(revisions).find((resource) => resource.Properties.FunctionName === 'bref-web-diagnostic');
  assert.ok(diagnostic);
  assert.equal(diagnostic.Properties.ServiceConfig.EnvironmentVariables.AWS_TEST_TOKEN, undefined,
    'the token is only given to the test function');

  template.hasResourceProperties('AWS::Lambda::WebFunctionEndpoint', {
    FunctionName: 'bref-web-test',
    EndpointType: 'HomeRegion',
    AuthType: 'ApplicationManaged',
    RevisionWeights: [{ RevisionId: { 'Fn::GetAtt': [Match.anyValue(), 'RevisionId'] }, Weight: 100 }],
    ScalingConfig: { MaxEnvironments: 10 },
    ThrottleConfig: { RateLimit: 200 },
  });
  template.hasResourceProperties('AWS::Logs::LogGroup', {
    LogGroupName: '/aws/lambda/web/bref-web-test',
    RetentionInDays: 7,
  });
  // Lambda's duplicate copy is owned too, so it expires and is destroyed.
  template.hasResourceProperties('AWS::Logs::LogGroup', {
    LogGroupName: '/aws/lambda/web/bref-web-test.local',
    RetentionInDays: 7,
  });
});

test('an SSM probe creates a readable parameter and a bref-ssm reference', () => {
  const archive = loadArchive(fakeArchive('laravel.zip', 'aarch64', 'lambda-web'), { requireLambdaWeb: true });
  const stack = new ExplorationStack(new App(), 'Probe', {
    apps: [{ id: 'laravel', archive, ssmProbeValue: 'resolved-by-bref-ssm' }],
    namePrefix: 'bref-web',
    maxInFlight: 4,
    timeoutSeconds: 30,
    maxEnvironments: 10,
    rateLimit: 100,
    logRetention: RetentionDays.ONE_WEEK,
    forwardedHeaders: 'ingress',
  });
  const template = Template.fromStack(stack);
  template.hasResourceProperties('AWS::SSM::Parameter', {
    Name: '/bref-web-laravel/probe',
    Value: 'resolved-by-bref-ssm',
  });
  template.hasResourceProperties('AWS::Lambda::WebFunctionRevision', {
    ServiceConfig: { EnvironmentVariables: Match.objectLike({
      PROBE_SSM: 'bref-ssm:/bref-web-laravel/probe',
      BREF_WEB_FORWARDED_HEADERS: 'ingress',
    }) },
  });
  template.hasResourceProperties('AWS::IAM::Policy', {
    PolicyDocument: { Statement: Match.arrayWith([Match.objectLike({
      Action: Match.arrayWith(['ssm:GetParameters']),
    })]) },
  });
});

test('benchmark apps: per-function concurrency and a Bref event function with a Function URL', () => {
  const archive = loadArchive(fakeArchive('laravel-bench.zip', 'aarch64', 'lambda-web'), { requireLambdaWeb: true });
  const code = mkdtempSync(join(tmpdir(), 'bref-web-event-'));
  writeFileSync(join(code, 'index.php'), '<?php');
  const stack = new ExplorationStack(new App(), 'Bench', {
    apps: [{ id: 'bench-c16', archive, maxInFlight: 16 }],
    eventApps: [{ id: 'bench-event', codeDirectory: code, memorySize: 1024,
      layerArn: 'arn:aws:lambda:us-west-1:873528684822:layer:arm-php-84:25' }],
    namePrefix: 'bref-web',
    maxInFlight: 4,
    timeoutSeconds: 30,
    maxEnvironments: 10,
    rateLimit: 100,
    logRetention: RetentionDays.ONE_WEEK,
  });
  const template = Template.fromStack(stack);
  template.hasResourceProperties('AWS::Lambda::WebFunctionRevision', {
    ServiceConfig: { MaxConcurrencyPerEnvironment: 16, EnvironmentVariables: Match.objectLike({ BREF_WEB_MAX_IN_FLIGHT: '16' }) },
  });
  // As Bref's serverless plugin configures a php-84-fpm function on ARM64.
  template.hasResourceProperties('AWS::Lambda::Function', {
    FunctionName: 'bref-web-bench-event',
    Runtime: 'provided.al2023',
    Architectures: ['arm64'],
    Handler: 'public/index.php',
    MemorySize: 1024,
    Layers: ['arn:aws:lambda:us-west-1:873528684822:layer:arm-php-84:25'],
    Environment: { Variables: { BREF_RUNTIME: 'Bref\\FpmRuntime\\Main' } },
  });
  template.hasResourceProperties('AWS::Lambda::Url', { AuthType: 'NONE' });
});
