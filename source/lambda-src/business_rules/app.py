"""Business rules engine implemented as a Lambda durable function.

Mirrors the Step Function workflow for payment transaction processing:
  1. Validate the transaction (reject if issuingCountryCode is empty/missing)
  2. Run three independent business rule checks in parallel
  3. Emit a TransactionPostingApproved event on the happy path

Triggered by EventBridge "TransactionEnriched" events. On validation failure,
publishes the full event envelope to SNS and returns early — no approval event
is emitted.

Durable execution guarantees:
  - Each step is checkpointed; on replay, completed steps are skipped.
  - Logging via context.logger / step_ctx.logger is replay-aware (no duplicates).
  - Uncaught exceptions outside steps mark the execution as FAILED immediately.
"""

import json
import os

import boto3
from aws_durable_execution_sdk_python import DurableContext, durable_execution

events_client = boto3.client("events")
sns_client = boto3.client("sns")

# Required environment variables — validated at the start of each invocation.
# Using .get() so the module loads cleanly; the handler raises if either is None.
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
EVENT_SOURCE = "octank.payments.posting.rules"

# The exact 16 fields the Step Function maps into every emitted event detail.
RULE_DETAIL_FIELDS = (
    "original_message", "iban", "account_type", "has_holds",
    "suspense_account", "head_office_account", "tax_category",
    "billingAmount", "transactionAmount", "conversionRate",
    "merchantType", "issuingCountryCode", "authCode",
    "acquiringCountryCode", "posEntryMode", "systemTraceAuditNumber",
)


def build_rule_detail(detail):
    """Extract only the 16 canonical fields from the transaction detail."""
    return {k: detail.get(k) for k in RULE_DETAIL_FIELDS}


def put_event(detail_type, detail):
    """Publish a business rule event to EventBridge."""
    events_client.put_events(
        Entries=[
            {
                "Source": EVENT_SOURCE,
                "DetailType": detail_type,
                "EventBusName": EVENT_BUS_NAME,
                "Detail": json.dumps(build_rule_detail(detail)),
            }
        ]
    )


# --- Business rule checks ---------------------------------------------------
# Each receives a StepContext (for replay-aware logging) and the transaction
# detail dict. These are standalone functions so the logic can grow over time.


def check_foreign_transaction(step_ctx, detail):
    """Emit ForeignTransactionFound when billing and transaction amounts differ."""
    billing = detail.get("billingAmount")
    transaction = detail.get("transactionAmount")
    step_ctx.logger.info(f"Checking foreign transaction: billingAmount={billing}, transactionAmount={transaction}")
    if billing != transaction:
        step_ctx.logger.info(f"Foreign transaction detected for {detail.get('systemTraceAuditNumber')}, publishing event")
        put_event("ForeignTransactionFound", detail)
    else:
        step_ctx.logger.info("No foreign transaction detected, passed")


def check_conversion_rate(step_ctx, detail):
    """Emit CurrencyConversionTransactionFound when conversionRate is '1'."""
    rate = detail.get("conversionRate")
    step_ctx.logger.info(f"Checking conversion rate: conversionRate={rate}")
    if rate == "1":
        step_ctx.logger.info(f"Currency conversion detected for {detail.get('systemTraceAuditNumber')}, publishing event")
        put_event("CurrencyConversionTransactionFound", detail)
    else:
        step_ctx.logger.info("No currency conversion detected, passed")


def check_merchant_type(step_ctx, detail):
    """Emit WarningMerchantTypeTransactionFound when merchantType is 'AAFF'."""
    merchant = detail.get("merchantType")
    step_ctx.logger.info(f"Checking merchant type: merchantType={merchant}")
    if merchant == "AAFF":
        step_ctx.logger.info(f"Warning merchant type detected for {detail.get('systemTraceAuditNumber')}, publishing event")
        put_event("WarningMerchantTypeTransactionFound", detail)
    else:
        step_ctx.logger.info("No warning merchant type detected, passed")


# --- Handler -----------------------------------------------------------------


@durable_execution
def lambda_handler(event, context: DurableContext):
    detail = event["detail"]
    txn_id = detail.get("systemTraceAuditNumber")

    # Fail fast on misconfiguration. Raising outside a step marks the durable
    # execution as FAILED, causing EventBridge to retry delivery. If retries
    # exhaust, the event lands in the target's DLQ.
    if EVENT_BUS_NAME is None or SNS_TOPIC_ARN is None:
        raise EnvironmentError("Missing required environment variables: EVENT_BUS_NAME and/or SNS_TOPIC_ARN")

    # Step 1: Validate transaction — reject if issuingCountryCode is empty or missing
    context.logger.info(f"Starting business rules processing for transaction {txn_id}")
    is_valid = context.step(
        lambda _: detail.get("issuingCountryCode", "") != "",
        name="validate-transaction",
    )

    if not is_valid:
        # Publish the full EventBridge envelope to SNS (matches Step Function behavior)
        context.logger.warning(f"Validation failed for transaction {txn_id}: missing issuingCountryCode")
        context.step(
            lambda _: sns_client.publish(
                TopicArn=SNS_TOPIC_ARN,
                Message=json.dumps(event),
            ),
            name="publish-posting-failure",
        )
        return {"systemTraceAuditNumber": txn_id, "status": "failed"}

    # Step 2: Run independent business rule checks in parallel.
    # Each check conditionally emits an event; none block the approval.
    context.logger.info(f"Validation passed for transaction {txn_id}, running business rules")
    context.parallel(
        functions=[
            lambda ctx: ctx.step(lambda sc: check_foreign_transaction(sc, detail), name="trigger-foreign-transaction-rule"),
            lambda ctx: ctx.step(lambda sc: check_conversion_rate(sc, detail), name="trigger-conversion-rate-rule"),
            lambda ctx: ctx.step(lambda sc: check_merchant_type(sc, detail), name="trigger-merchant-rule"),
        ],
        name="run-business-rules",
    )

    # Step 3: Emit approval event — always reached when validation passes
    context.logger.info(f"All business rules completed for transaction {txn_id}, posting approval")
    context.step(
        lambda _: put_event("TransactionPostingApproved", detail),
        name="post-transaction-processed",
    )

    context.logger.info(f"Transaction {txn_id} approved successfully")
    return {
        "systemTraceAuditNumber": txn_id,
        "status": "approved",
    }
