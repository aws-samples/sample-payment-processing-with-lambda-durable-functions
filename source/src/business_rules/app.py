from aws_durable_execution_sdk_python import (
    DurableContext,
    durable_execution,
    durable_step,
)
from aws_durable_execution_sdk_python.config import Duration


@durable_step
def validate_transaction(step_context, detail):
    step_context.logger.info(f"Validating transaction {detail['systemTraceAuditNumber']}")
    return {"status": "validated"}


@durable_step
def process_payment(step_context, detail):
    step_context.logger.info(f"Processing payment for {detail['systemTraceAuditNumber']}")
    return {"status": "paid", "amount": detail["billingAmount"]}


@durable_step
def confirm_transaction(step_context, detail):
    step_context.logger.info(f"Confirming transaction {detail['systemTraceAuditNumber']}")
    return {"status": "confirmed"}


@durable_execution
def lambda_handler(event, context: DurableContext):
    detail = event["detail"]

    validation_result = context.step(validate_transaction(detail))
    payment_result = context.step(process_payment(detail))

    context.wait(Duration.from_seconds(10))

    confirmation_result = context.step(confirm_transaction(detail))

    return {
        "systemTraceAuditNumber": detail["systemTraceAuditNumber"],
        "status": "completed",
        "steps": [validation_result, payment_result, confirmation_result],
    }
