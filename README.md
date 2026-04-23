# Building Payments Processing with Lambda Durable Functions

This sample demonstrates how to build a near real-time payment processing pipeline using [AWS Lambda Durable Functions](https://docs.aws.amazon.com/lambda/latest/dg/lambda-durable.html) and event-driven architecture on AWS.

## Overview

The solution implements a multi-stage payment processing workflow where a Visa authorization message flows through deduplication, enrichment, business rule evaluation, and settlement posting — each stage decoupled via Amazon EventBridge.

The business rules stage uses Lambda Durable Functions to orchestrate transaction validation and parallel business rule checks with automatic checkpointing, exactly-once execution, and failure isolation.

### Architecture

```
Visa Mock → DynamoDB → EventBridge Pipes → Dedup → EventBridge
    → Enrich → EventBridge → Business Rules (Durable) → EventBridge
    → SQS FIFO → Posting
```

### Pipeline Stages

| Stage | Lambda Function | Description |
|-------|----------------|-------------|
| Ingest | `payments-visa-mock` | Reads sample Visa authorization messages from CSV and writes to DynamoDB |
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
pip3 install -r requirements-test.txt
pytest test_app.py -v
```

This runs 10 unit tests covering transaction validation, business rule checks, event schema validation, and misconfiguration handling. All tests use the AWS Durable Execution Testing SDK to run the handler locally without deployed AWS resources.

Return to the source directory:

```bash
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

On successful completion, Terraform outputs the DynamoDB stream ARN:

```
Apply complete! Resources: N added, 0 changed, 0 destroyed.

Outputs:
  stream_arn = "arn:aws:dynamodb:us-east-2:xxxxxxxxxxxx:table/visa/stream/..."
```

### Step 4: Verify Durable Function Configuration

In the AWS Lambda console, navigate to the `payments-business-rules` function. Confirm that the function **Type** displays as **Durable** and a `durable` alias is configured.

### Step 5: Execute a Test Payment

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

### Step 6: Verify Results

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
