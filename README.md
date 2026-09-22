# Building Payments Processing with Lambda Durable Functions

This sample demonstrates how to build a near real-time payment processing pipeline using [AWS Lambda Durable Functions](https://docs.aws.amazon.com/lambda/latest/dg/lambda-durable.html) and event-driven architecture on AWS.

## Overview

The solution implements a multi-stage payment processing workflow where a payment authorization message flows through deduplication, enrichment, business rule evaluation, and settlement posting — each stage decoupled via Amazon EventBridge.

The business rules stage uses Lambda Durable Functions to orchestrate transaction validation and parallel business rule checks with automatic checkpointing, exactly-once execution, and failure isolation.

### Architecture

```
Mock Payment → DynamoDB → EventBridge Pipes → Dedup → EventBridge
    → Enrich → EventBridge → Business Rules (Durable) → EventBridge
    → SQS FIFO → Posting
```

### Pipeline Stages

| Stage | Lambda Function | Description |
|-------|----------------|-------------|
| Ingest | `payments-visa-mock` | Reads sample payment authorization messages from CSV and writes to DynamoDB |
| Dedup | `payments-dedup` | Conditional writes to DynamoDB to detect duplicate transactions within a time window |
| Enrich | `payments-enrich` | Enriches transactions with account details (IBAN, account type, holds, tax category) |
| Business Rules | `payments-business-rules` | **Durable function** — validates transactions and runs parallel business rule checks |
| Posting | `payments-posting` | Processes approved transactions from SQS FIFO queue for settlement |
| FX Checker | `payments-fxchecker` | Foreign transaction detection utility |

### Durable Function Features Used

The `payments-business-rules` Lambda leverages four core durable function capabilities:

- **`context.step`** — Wraps each processing stage (validate, apply business rules, emit approval) with automatic checkpointing
- **`context.parallel`** — Executes three independent business rule checks concurrently (foreign transaction, currency conversion, merchant type)
- **`@durable_execution`** — Decorator that enables the checkpoint-and-replay mechanism
- **`context.logger`** — Replay-aware logging that emits entries only once, not on every replay

### Encryption with Customer Managed Keys (CMK)

Durable function checkpoints persist execution state — step results, payloads, and callback responses — to durable storage. For payment processing workloads, this data is sensitive. The `payments-business-rules` durable function supports encrypting this checkpointed data with a customer managed key (CMK) from AWS KMS instead of the AWS owned default key.

A CMK gives you three controls:

- You set the key rotation schedule.
- You restrict decryption access through the key policy, scoped to the Lambda service, the function's execution role, the function author, and (optionally) durable execution operators.
- You get per-function audit trails of `Decrypt` and `GenerateDataKey` calls in AWS CloudTrail.

A durable execution uses the key it started with for its entire lifetime — changing or removing the key only affects executions that start afterward. Removing decrypt permissions from the key policy pauses in-flight executions at their next checkpoint until access is restored; scheduling key deletion is permanent and makes any execution encrypted with that key unrecoverable, so use the KMS waiting period (7–30 days) and check AWS CloudTrail for `Decrypt` activity before deleting a key.

The CMK and its key policy are provisioned in [`source/durable_kms.tf`](source/durable_kms.tf) using the [`terraform-aws-modules/kms/aws`](https://registry.terraform.io/modules/terraform-aws-modules/kms/aws/latest) module. The key policy grants exactly the AWS KMS actions each principal needs:

| Principal | Actions | Purpose |
|-----------|---------|---------|
| Account root | `kms:*` | Standard IAM user permissions for key administration |
| `lambda.amazonaws.com` | `kms:GenerateDataKey`, `kms:Decrypt` | Lets the Lambda service encrypt/decrypt checkpoint data, scoped to this function via `SourceAccount`, `SourceArn`, and `EncryptionContext` conditions |
| Function execution role | `kms:Decrypt` | Lets the running function decrypt state to progress an execution |
| Function author | `kms:DescribeKey`, `kms:GenerateDataKey`, `kms:Decrypt` | Lets Lambda validate the key (symmetric, enabled, correct permissions) during `CreateFunction`/`UpdateFunctionConfiguration` |
| Durable execution operators (optional) | `kms:Decrypt` | Lets an operator role inspect execution state, only added if `durable_operator_role_arn` is set |

Two Terraform variables control the key policy:

| Variable | Description | Default |
|----------|--------------|---------|
| `function_author_role_arn` | The IAM principal (role or user) granted function-author permissions in the CMK key policy - the identity Lambda validates the key against when the CMK is associated with the function. Must match whoever performs that association (normally the deployer). Defaults to the principal running `terraform apply`, resolved to the underlying role ARN for an assumed-role/SSO session or the user ARN for an IAM user. Override it to authorize a *different* author. | Deploying principal (auto-resolved via `aws_iam_session_context`) |
| `durable_operator_role_arn` | ARN of an IAM role for durable execution operators | `null` (statement omitted) |

> **Terraform limitation:** Terraform does not yet support attaching a KMS CMK directly to a Lambda durable function's encryption configuration. `terraform apply` provisions the key and policy, but you must associate the key with the function manually in the Lambda console (or via AWS CLI) as a follow-up step — see below.

#### Associating the CMK with the durable function

1. Deploy the infrastructure as described in [Getting Started](#getting-started). Note the `durable_kms_key_arn` and `durable_kms_key_alias` outputs.
2. In the AWS Lambda console, open the `payments-business-rules` function and confirm its **Type** displays **Durable**.
3. Open the **Durable executions** tab. In the **Durable configuration** panel, choose **Edit**.
4. On the **Edit durable configuration settings** page, under **Encryption**, select **Customize encryption settings**.
5. In the key search box, select the key aliased `durable-function-encryption` (the ARN from the `durable_kms_key_arn` output), then choose **Save**. The **Durable configuration** panel now shows your CMK under **AWS KMS customer managed key ARN** instead of the default AWS owned key.

Verify the association via AWS CLI:

```bash
aws lambda get-function-configuration \
  --function-name payments-business-rules \
  --query "DurableConfig"
```

Expected response:

```json
{
    "KMSKeyArn": "arn:aws:kms:us-east-2:xxxxxxxxxxxx:key/4e87d4c2-1190-4db4-8b97-46657f83ee00",
    "RetentionPeriodInDays": 7,
    "ExecutionTimeout": 180
}
```

You can also trace the key usage in AWS CloudTrail. When Lambda validates or updates the CMK on the durable function, it issues dry-run `GenerateDataKey` and `Decrypt` calls that appear in CloudTrail with a `DryRunOperationException` error code — this confirms the key policy permissions are correct and is not an actual error.

For background on the encryption model, see [Encrypting AWS Lambda durable execution data](https://docs.aws.amazon.com/lambda/latest/dg/durable-encryption.html) in the AWS Lambda Developer Guide.

## Prerequisites

1. **AWS Account and CLI** — An active AWS account with the [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) installed and configured with appropriate credentials.

2. **Terraform** — [Terraform](https://developer.hashicorp.com/terraform/install) version 1.0 or later.

3. **Python** — Python 3.11 or later with pip. The `aws-durable-execution-sdk-python` package requires Python 3.11+.

4. **IAM Permissions** — Sufficient permissions to create Lambda functions, DynamoDB tables, EventBridge event buses and rules, EventBridge Pipes, SQS queues, SNS topics, KMS keys, and associated IAM roles. The durable function Lambda role requires the `AWSLambdaBasicDurableExecutionRolePolicy` managed policy.

5. **Shell Environment** — The Terraform provisioners use Python and `pip3`. Ensure Python 3.11+ and pip are on your system PATH. Works on Windows, Linux, and macOS.

## Repository Structure

```
├── README.md
├── source/
│   ├── main.tf                    # Root Terraform configuration
│   ├── variables.tf               # Input variables
│   ├── outputs.tf                 # Terraform outputs
│   ├── durable_kms.tf             # CMK and key policy for durable function encryption
│   ├── lambda_function/           # Reusable Lambda module
│   │   ├── main.tf
│   │   └── variables.tf
│   ├── lambda-src/                # Lambda function source code
│   │   ├── business-rules/        # Durable function (business rules engine)
│   │   │   ├── app.py
│   │   │   ├── test_app.py
│   │   │   ├── requirements.txt
│   │   │   └── requirements-test.txt
│   │   ├── dedup/                 # Deduplication Lambda
│   │   ├── enrich/                # Transaction enrichment Lambda
│   │   ├── posting/               # Settlement posting Lambda
│   │   ├── visa-mock/             # Mock Visa authorization Lambda
│   │   └── fxchecker/             # Foreign exchange checker Lambda
│   ├── dynamodb/                  # DynamoDB module
│   ├── event_bridge/              # EventBridge module (bus, rules, targets)
│   ├── event-pipes/               # EventBridge Pipes module
│   ├── sqs/                       # SQS module (FIFO queues + DLQs)
│   ├── sns/                       # SNS module (validation failure notifications)
│   └── env/                       # Environment-specific CI/CD configs
```

## Getting Started

### Step 1: Clone the Repository

```bash
git clone https://github.com/aws-samples/sample-payment-processing-with-lambda-durable-functions.git
cd sample-payment-processing-with-lambda-durable-functions/source
```

### Step 2: Run Unit Tests (Optional)

Validate the business rules logic locally before deploying:

```bash
cd lambda-src/business-rules
python3 -m venv .venv
source .venv/bin/activate
pip3 install -r requirements-test.txt
pytest test_app.py -v
```

This runs 10 unit tests covering transaction validation, business rule checks, event schema validation, and misconfiguration handling. All tests use the AWS Durable Execution Testing SDK to run the handler locally without deployed AWS resources.

Deactivate the virtual environment and return to the source directory:

```bash
deactivate
cd ../../
```

### Step 3: Deploy with Terraform

Initialize Terraform and review the plan:

```bash
terraform init
terraform plan -var="region=us-east-2"
```

Deploy the infrastructure:

```bash
terraform apply -var="region=us-east-2" --auto-approve
```

> **Note:** Replace `us-east-2` with your preferred AWS Region. The default region is `eu-west-1` if not specified.

On successful completion, Terraform outputs the AWS KMS key alias and ARN for the durable function's CMK, along with the DynamoDB stream ARN:

```
Apply complete! Resources: N added, 0 changed, 0 destroyed.

Outputs:
  durable_kms_key_alias = "durable-function-encryption"
  durable_kms_key_arn   = "arn:aws:kms:us-east-2:xxxxxxxxxxxx:key/4e87d4c2-1190-4db4-8b97-46657f83ee00"
  stream_arn            = "arn:aws:dynamodb:us-east-2:xxxxxxxxxxxx:table/visa/stream/..."
```

### Step 4: Verify Durable Function Configuration

In the AWS Lambda console, navigate to the `payments-business-rules` function. Confirm that the function **Type** displays as **Durable** and a `durable` alias is configured.

### Step 5: Associate the CMK with the Durable Function

Terraform provisions the CMK and its key policy, but attaching the key to the durable function's encryption configuration is a manual, one-time step in the Lambda console (Terraform does not yet support this natively). Follow [Associating the CMK with the durable function](#associating-the-cmk-with-the-durable-function) above, using the `durable_kms_key_arn` output from Step 3.

### Step 6: Execute a Test Payment

Invoke the mock payment function to trigger the end-to-end pipeline:

```bash
aws lambda invoke --function-name payments-visa-mock --output json /dev/stdout
```

Expected response:

```json
{
  "statusCode": 200,
  "body": "\"Hello from Lambda!\""
}
```

This triggers the following pipeline:

1. The `visa-mock` Lambda writes authorization records to the DynamoDB `visa` table.
2. DynamoDB Streams captures the changes, and EventBridge Pipes forwards them through the `dedup` Lambda to EventBridge as `TransactionAuthorized` events.
3. EventBridge routes `TransactionAuthorized` events to the `enrich` Lambda, which enriches the transaction with account details and emits `TransactionEnriched` events.
4. EventBridge routes `TransactionEnriched` events to the `business-rules` durable Lambda, which:
   - Validates the transaction (checks `issuingCountryCode` is present)
   - Runs three business rule checks in parallel (foreign transaction, currency conversion, merchant type)
   - Emits a `TransactionPostingApproved` event on the happy path
5. EventBridge routes `TransactionPostingApproved` events to an SQS FIFO queue, which triggers the `posting` Lambda for settlement.

### Step 7: Verify Results

Open Amazon CloudWatch Logs and inspect the log group `/aws/lambda/payments-business-rules`. You should see:

- `Starting business rules processing for transaction [TXN-ID]`
- `Validation passed for transaction [TXN-ID], running business rules`
- Individual business rule check results (foreign transaction, conversion rate, merchant type)
- `All business rules completed for transaction [TXN-ID], posting approval`
- `Transaction [TXN-ID] approved successfully`

Additional log groups to trace the full pipeline:

| Log Group | Description |
|-----------|-------------|
| `/aws/lambda/payments-enrich` | Transaction enrichment logs |
| `/aws/lambda/payments-posting` | Settlement posting logs |
| `/aws/events/ForeignTransactions` | Foreign transaction detection events |
| `/aws/events/CurrencyConversionTransaction` | Currency conversion events |

## Clean Up

To avoid ongoing charges, destroy all deployed resources:

```bash
terraform destroy -var="region=us-east-2" --auto-approve
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
