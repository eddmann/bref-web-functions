# AWS exploration stack (CDK)

Deploys the CI-tested exploration archives as Lambda Web Functions for the
[AWS test plan](../../docs/aws-test-plan.md). **Use an isolated account.**

One stack, `BrefWebExploration`, creates:

| Resource | Purpose |
| --- | --- |
| Versioned S3 bucket | Web Functions read code from a versioned bucket readable by `lambda.amazonaws.com` |
| Execution role | Trusted by `lambda.amazonaws.com`; may write logs under `/aws/lambda/web/*` |
| Per archive: `AWS::Lambda::WebFunction` | `bref-web-diagnostic` and `bref-web-test` (and `bref-web-laravel`, `bref-web-symfony` when selected) |
| Per archive: `AWS::Lambda::WebFunctionRevision` | `nodejs24.x`, the ZIP's root `index.js`, pinned to the uploaded S3 object version |
| Per archive: `AWS::Lambda::WebFunctionEndpoint` | `HomeRegion`, public (`ApplicationManaged`), all traffic to the revision |
| Per archive: log groups | `/aws/lambda/web/<function>` and Lambda's duplicate `<function>.local`, both with retention (default 7 days) |
| Laravel only: SSM parameter | `/bref-web-laravel/probe`, readable by the execution role, passed as `PROBE_SSM=bref-ssm:...` |

The archive is uploaded byte-for-byte: `bin/app.ts` refuses a ZIP whose SHA-256
differs from its `.sha256`, or whose manifest is not `aarch64`/`lambda-web`.

## Runtime settings derived by the stack

| Context (`-c`) | Default | Sets |
| --- | --- | --- |
| `maxInFlight` | `12` | `BREF_WEB_MAX_IN_FLIGHT` **and** `maxConcurrencyPerEnvironment`. Lambda's default is 64, far above twelve PHP workers, so this keeps AWS from routing more requests than PHP can serve |
| `timeoutSeconds` | `30` | Lambda's request timeout; `BREF_WEB_REQUEST_TIMEOUT_MS` equals it and `BREF_WEB_FPM_TIMEOUT_SECONDS` is two seconds lower. FPM ends overruns up to a third late, so Lambda's own 504 usually answers; the gateway waits for FPM instead of retiring |
| `maxEnvironments` | `10` | Endpoint scaling cap (cost and blast radius); each environment is 2 vCPU / 2 GB |
| `rateLimit` | `100` | Requests per second per endpoint |
| `apps` | `diagnostic,test` | Which archives to deploy; add `laravel,symfony` for case 14 (`laravel.zip`, `symfony.zip` packaged locally, see the test plan) |
| `testToken` | required for `test`, `laravel`, `symfony` | `AWS_TEST_TOKEN`: the test-only routes need header `X-Aws-Test-Token`. The framework apps' `APP_KEY`/`APP_SECRET` are derived from it |
| `archives` | `./archives` | Directory holding the ZIPs and `.sha256` files |
| `forwardedHeaders` | unset (runtime default `passthrough`) | `BREF_WEB_FORWARDED_HEADERS` for every function; `ingress` when nothing sits in front of the endpoints |
| `logRetentionDays` | `7` | Log group retention |

## Deploy

Requires Node.js 22+, `unzip`, AWS credentials for the test account, and a
Region where Lambda Web Functions are available.

```sh
cd infra/aws
npm ci
npm test

# The tested ARM64 archives from a CI run on main:
gh run download <run-id> --name aws-archives-aarch64 --dir archives

npx cdk bootstrap                       # once per account and Region
npx cdk deploy -c testToken="$(openssl rand -hex 24)"
```

Keep the token: the outputs list each endpoint URL, function name, log group and
archive SHA-256. Record the SHA-256 with every result.

```sh
curl -s https://<diagnostic-domain>/ | jq .
curl -s -H "X-Aws-Test-Token: $TOKEN" "https://<test-domain>/aws-test/stream?chunks=5&interval_ms=1000"
```

`npx cdk destroy` (with the same `-c` values) removes everything, including the
bucket and both log groups per function. Lambda can recreate
`/aws/lambda/web/<function>` from a late log write during deletion, and the CDK
custom resources leave `/aws/lambda/BrefWebExploration-Custom*` groups; delete
those separately.

## Verified on AWS

Deployed on ARM64 in us-west-1 (see the test plan for the cases):

- CloudFormation accepts the new resource types; the CDK CLI's local
  `Unknown resource type` warnings are expected.
- Revisions accept the pinned S3 `versionId` and the bucket policy; the
  execution role's log and SSM permissions are sufficient.
- Updating to new archives creates a new revision and moves the endpoint to it
  with no failed requests (265/265 at one request per second per endpoint); the
  old revision is removed.
- The endpoint URL output serves the diagnostic page.
- Lambda spreads load across environments rather than filling each: 50
  requests/s of sub-millisecond PHP used all 10 environments (7 of 3000
  throttled with `Platform.EndpointMaxEnvThrottle`). Size `maxEnvironments` for
  the request rate, not only for concurrency.
