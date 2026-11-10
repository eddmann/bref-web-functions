import { ArnFormat, CfnOutput, Duration, Fn, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { Archive } from './archives';

/** One archive deployed as its own Web Function with one endpoint. */
export interface WebFunctionApp {
  /** Short lowercase name, e.g. `diagnostic`; the function is `<prefix>-<id>`. */
  readonly id: string;
  readonly archive: Archive;
  /** Extra environment variables for this function only. */
  readonly environment?: Record<string, string>;
  /**
   * Create an SSM parameter with this value, readable by the function, and set
   * `PROBE_SSM=bref-ssm:<name>` so Bref's secrets loader resolves it at startup.
   */
  readonly ssmProbeValue?: string;
  /** PHP-FPM workers and maxConcurrencyPerEnvironment for this function; defaults to the stack's. */
  readonly maxInFlight?: number;
}

/**
 * The same application as a classic (event) Lambda function with Bref's FPM
 * layer behind a Function URL, as Bref deploys it, for comparison benchmarks.
 */
export interface EventFunctionApp {
  readonly id: string;
  /** Application directory, zipped as the function code (no runtime: the layer provides it). */
  readonly codeDirectory: string;
  /** Bref's PHP layer ARN for the Region and architecture. */
  readonly layerArn: string;
  readonly memorySize: number;
  readonly environment?: Record<string, string>;
}

export interface ExplorationStackProps extends StackProps {
  readonly apps: WebFunctionApp[];
  /** Classic Lambda functions with Bref's layer, for comparison. */
  readonly eventApps?: EventFunctionApp[];
  /** Function name prefix. */
  readonly namePrefix: string;
  /**
   * PHP-FPM workers per environment. Also Lambda's maxConcurrencyPerEnvironment,
   * so Lambda never routes more concurrent requests than there are workers.
   */
  readonly maxInFlight: number;
  /** Lambda's per-request timeout; the gateway and FPM deadlines are derived from it. */
  readonly timeoutSeconds: number;
  /** Upper bound on environments per endpoint (cost and blast-radius limit). */
  readonly maxEnvironments: number;
  /** Requests per second per endpoint. */
  readonly rateLimit: number;
  readonly logRetention: logs.RetentionDays;
  /** `BREF_WEB_FORWARDED_HEADERS` for every function; unset keeps the runtime default (passthrough). */
  readonly forwardedHeaders?: 'passthrough' | 'ingress';
}

const RUNTIME = 'nodejs24.x';

export function validateSettings(props: Pick<ExplorationStackProps, 'maxInFlight' | 'timeoutSeconds' | 'maxEnvironments' | 'rateLimit'>): void {
  const { maxInFlight, timeoutSeconds, maxEnvironments, rateLimit } = props;
  if (!Number.isInteger(maxInFlight) || maxInFlight < 1 || maxInFlight > 128) {
    throw new Error('maxInFlight must be an integer from 1 to 128 (BREF_WEB_MAX_IN_FLIGHT and maxConcurrencyPerEnvironment)');
  }
  // FPM's script limit is two seconds below the request timeout, and must stay positive.
  if (!Number.isInteger(timeoutSeconds) || timeoutSeconds < 5 || timeoutSeconds > 900) {
    throw new Error('timeoutSeconds must be an integer from 5 to 900');
  }
  if (!Number.isInteger(maxEnvironments) || maxEnvironments < 2 || maxEnvironments > 10000) {
    throw new Error('maxEnvironments must be an integer from 2 to 10000');
  }
  const validRate = rateLimit <= 1000 ? rateLimit % 100 === 0 : rateLimit % 1000 === 0 && rateLimit <= 10000;
  if (!Number.isInteger(rateLimit) || rateLimit < 100 || !validRate) {
    throw new Error('rateLimit must be 100-1000 in steps of 100, or 2000-10000 in steps of 1000 (0 would block all traffic)');
  }
}

/**
 * Deploys tested runtime archives as Lambda Web Functions for the AWS test plan
 * (docs/aws-test-plan.md): a versioned code bucket, an execution role, and per
 * archive a function, an immutable revision pinned to the uploaded object
 * version, a log group with retention, and a HomeRegion endpoint.
 */
export class ExplorationStack extends Stack {
  constructor(scope: Construct, id: string, props: ExplorationStackProps) {
    super(scope, id, props);
    validateSettings(props);
    if (props.apps.length + (props.eventApps?.length ?? 0) === 0) {
      throw new Error('No apps selected');
    }

    // Web Functions read code from a versioned bucket that grants Lambda read access.
    const bucket = new s3.Bucket(this, 'Code', {
      versioned: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });
    bucket.addToResourcePolicy(new iam.PolicyStatement({
      principals: [new iam.ServicePrincipal('lambda.amazonaws.com')],
      actions: ['s3:GetObject', 's3:GetObjectVersion'],
      resources: [bucket.arnForObjects('*')],
    }));

    const role = new iam.Role(this, 'ExecutionRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for the bref-web exploration Web Functions',
    });
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      // Covers the function's log group, its streams and Lambda's ".local" copies.
      resources: [
        this.formatArn({
          service: 'logs',
          resource: 'log-group',
          resourceName: '/aws/lambda/web/*',
          arnFormat: ArnFormat.COLON_RESOURCE_NAME,
        }),
      ],
    }));

    for (const app of props.apps) {
      this.addWebFunction(app, bucket, role, props);
    }
    for (const app of props.eventApps ?? []) {
      this.addEventFunction(app, props);
    }
  }

  private addEventFunction(app: EventFunctionApp, props: ExplorationStackProps): void {
    const functionName = `${props.namePrefix}-${app.id}`;
    const scope = new Construct(this, app.id);
    const logGroup = new logs.LogGroup(scope, 'Logs', {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: props.logRetention,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    // As Bref's serverless plugin configures an FPM function: provided.al2023,
    // its PHP layer and BREF_RUNTIME selecting the FPM runtime.
    const fn = new lambda.Function(scope, 'Function', {
      functionName,
      runtime: lambda.Runtime.PROVIDED_AL2023,
      architecture: lambda.Architecture.ARM_64,
      handler: 'public/index.php',
      code: lambda.Code.fromAsset(app.codeDirectory),
      layers: [lambda.LayerVersion.fromLayerVersionArn(scope, 'Php', app.layerArn)],
      memorySize: app.memorySize,
      timeout: Duration.seconds(props.timeoutSeconds),
      logGroup,
      environment: { BREF_RUNTIME: 'Bref\\FpmRuntime\\Main', ...app.environment },
    });
    const url = fn.addFunctionUrl({ authType: lambda.FunctionUrlAuthType.NONE });
    new CfnOutput(scope, 'Url', { value: url.url, description: `${functionName} Function URL` });
    new CfnOutput(scope, 'FunctionName', { value: functionName });
    new CfnOutput(scope, 'LogGroup', { value: logGroup.logGroupName });
  }

  private addWebFunction(app: WebFunctionApp, bucket: s3.IBucket, role: iam.Role, props: ExplorationStackProps): void {
    if (!/^[a-z][a-z0-9-]{0,30}$/.test(app.id)) {
      throw new Error(`App id must be lowercase letters, digits and hyphens: ${app.id}`);
    }
    const functionName = `${props.namePrefix}-${app.id}`;
    const maxInFlight = app.maxInFlight ?? props.maxInFlight;
    validateSettings({ ...props, maxInFlight });
    const prefix = `${app.id}/`;
    const scope = new Construct(this, app.id);

    // Upload the exact CI archive unchanged; a .zip asset is not repacked.
    const upload = new s3deploy.BucketDeployment(scope, 'Archive', {
      sources: [s3deploy.Source.asset(app.archive.path)],
      destinationBucket: bucket,
      destinationKeyPrefix: prefix,
      extract: false,
      retainOnDelete: false,
    });

    const logGroup = new logs.LogGroup(scope, 'Logs', {
      logGroupName: `/aws/lambda/web/${functionName}`,
      retention: props.logRetention,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    // Lambda also writes every line to `<group>.local`, creating it without
    // retention if it does not exist. Own it so it expires and is destroyed.
    const localLogGroup = new logs.LogGroup(scope, 'LocalLogs', {
      logGroupName: `/aws/lambda/web/${functionName}.local`,
      retention: props.logRetention,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const environment = { ...app.environment };
    let ssmProbe: ssm.StringParameter | undefined;
    if (app.ssmProbeValue !== undefined) {
      const parameterName = `/${functionName}/probe`;
      ssmProbe = new ssm.StringParameter(scope, 'SsmProbe', { parameterName, stringValue: app.ssmProbeValue });
      ssmProbe.grantRead(role);
      environment.PROBE_SSM = `bref-ssm:${parameterName}`;
    }

    const webFunction = new lambda.CfnWebFunction(scope, 'Function', { functionName });

    const timeoutMs = props.timeoutSeconds * 1000;
    const revision = new lambda.CfnWebFunctionRevision(scope, 'Revision', {
      functionName,
      description: `${app.id} archive sha256 ${app.archive.sha256}`,
      buildConfig: {
        // The ZIP's root index.js is the runtime's default entry file.
        runtimeConfig: { runtime: RUNTIME },
        codeConfig: {
          s3Object: {
            bucket: bucket.bucketName,
            key: Fn.join('', [prefix, Fn.select(0, upload.objectKeys)]),
            versionId: Fn.select(0, upload.objectVersionIds),
          },
        },
      },
      serviceConfig: {
        executionRoleArn: role.roleArn,
        maxConcurrencyPerEnvironment: maxInFlight,
        timeoutSeconds: props.timeoutSeconds,
        environmentVariables: {
          BREF_WEB_MAX_IN_FLIGHT: String(maxInFlight),
          // Lambda's own 504 arrives at about the timeout (measured: the same
          // instant as this deadline), so the client usually sees Lambda's 504.
          BREF_WEB_REQUEST_TIMEOUT_MS: String(timeoutMs),
          BREF_WEB_FPM_TIMEOUT_SECONDS: String(props.timeoutSeconds - 2),
          ...(props.forwardedHeaders ? { BREF_WEB_FORWARDED_HEADERS: props.forwardedHeaders } : {}),
          ...environment,
        },
      },
    });
    revision.node.addDependency(webFunction);
    revision.node.addDependency(logGroup);
    revision.node.addDependency(localLogGroup);
    revision.node.addDependency(role);
    if (ssmProbe) {
      revision.node.addDependency(ssmProbe);
    }

    const endpoint = new lambda.CfnWebFunctionEndpoint(scope, 'Endpoint', {
      functionName,
      endpointName: 'test',
      endpointType: 'HomeRegion',
      // Public: header behaviour is part of the test. Test-only routes need a token.
      authType: 'ApplicationManaged',
      revisionWeights: [{ revisionId: revision.attrRevisionId, weight: 100 }],
      scalingConfig: { maxEnvironments: props.maxEnvironments },
      throttleConfig: { rateLimit: props.rateLimit },
    });
    endpoint.node.addDependency(webFunction);

    new CfnOutput(scope, 'Url', { value: Fn.join('', ['https://', endpoint.attrDomainName, '/']), description: `${functionName} endpoint` });
    new CfnOutput(scope, 'FunctionName', { value: functionName });
    new CfnOutput(scope, 'LogGroup', { value: logGroup.logGroupName });
    new CfnOutput(scope, 'ArchiveSha256', { value: app.archive.sha256 });
  }
}
