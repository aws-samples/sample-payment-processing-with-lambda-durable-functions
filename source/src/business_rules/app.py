import json
import os

import boto3
from aws_durable_execution_sdk_python import DurableContext, durable_execution

events_client = boto3.client("events")
sns_client = boto3.client("sns")

EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
EVENT_SOURCE = "octank.payments.posting.rules"

RULE_DETAIL_FIELDS = (
    "original_message", "iban", "account_type", "has_holds",
    "suspense_account", "head_office_account", "tax_category",
    "billingAmount", "transactionAmount", "conversionRate",
    "merchantType", "issuingCountryCode", "authCode",
    "acquiringCountryCode", "posEntryMode", "systemTraceAuditNumber",
)


def build_rule_detail(detail):
    return {k: detail.get(k) for k in RULE_DETAIL_FIELDS}


def put_event(detail_type, detail):
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


def check_foreign_transaction(step_ctx, detail):
    billing = detail.get("billingAmount")
    transaction = detail.get("transactionAmount")
    step_ctx.logger.info(f"Checking foreign transaction: billingAmount={billing}, transactionAmount={transaction}")
    if billing != transaction:
        step_ctx.logger.info(f"Foreign transaction detected for {detail.get('systemTraceAuditNumber')}, publishing event")
        put_event("ForeignTransactionFound", detail)
    else:
        step_ctx.logger.info("No foreign transaction detected, passed")


def check_conversion_rate(step_ctx, detail):
    rate = detail.get("conversionRate")
    step_ctx.logger.info(f"Checking conversion rate: conversionRate={rate}")
    if rate == "1":
        step_ctx.logger.info(f"Currency conversion detected for {detail.get('systemTraceAuditNumber')}, publishing event")
        put_event("CurrencyConversionTransactionFound", detail)
    else:
        step_ctx.logger.info("No currency conversion detected, passed")


def check_merchant_type(step_ctx, detail):
    merchant = detail.get("merchantType")
    step_ctx.logger.info(f"Checking merchant type: merchantType={merchant}")
    if merchant == "AAFF":
        step_ctx.logger.info(f"Warning merchant type detected for {detail.get('systemTraceAuditNumber')}, publishing event")
        put_event("WarningMerchantTypeTransactionFound", detail)
    else:
        step_ctx.logger.info("No warning merchant type detected, passed")


@durable_execution
def lambda_handler(event, context: DurableContext):
    detail = event["detail"]
    txn_id = detail.get("systemTraceAuditNumber")

    if EVENT_BUS_NAME is None or SNS_TOPIC_ARN is None:
        raise EnvironmentError("Missing required environment variables: EVENT_BUS_NAME and/or SNS_TOPIC_ARN")

    # Step 1: Validate transaction — fail fast on missing issuing country
    context.logger.info(f"Starting business rules processing for transaction {txn_id}")
    is_valid = context.step(
        lambda _: detail.get("issuingCountryCode", "") != "",
        name="validate-transaction",
    )

    if not is_valid:
        context.logger.warning(f"Validation failed for transaction {txn_id}: missing issuingCountryCode")
        context.step(
            lambda _: sns_client.publish(
                TopicArn=SNS_TOPIC_ARN,
                Message=json.dumps(event),
            ),
            name="publish-posting-failure",
        )
        return {"systemTraceAuditNumber": txn_id, "status": "failed"}

    # Step 2: Run independent business rules in parallel
    context.logger.info(f"Validation passed for transaction {txn_id}, running business rules")
    context.parallel(
        lambda ctx: ctx.step(lambda sc: check_foreign_transaction(sc, detail), name="trigger-foreign-transaction-rule"),
        lambda ctx: ctx.step(lambda sc: check_conversion_rate(sc, detail), name="trigger-conversion-rate-rule"),
        lambda ctx: ctx.step(lambda sc: check_merchant_type(sc, detail), name="trigger-merchant-rule"),
    )

    # Step 3: Post final approval event with explicit detail matching step function schema
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
