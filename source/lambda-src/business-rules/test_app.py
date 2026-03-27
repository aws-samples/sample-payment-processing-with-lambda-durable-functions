"""Local tests for the business-rules durable function.

Uses the AWS Durable Execution Testing SDK to run the handler locally
without deployed resources. Mocks boto3 clients so no AWS calls are made.

Run:  pytest test_app.py -v
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from aws_durable_execution_sdk_python.execution import InvocationStatus
from aws_durable_execution_sdk_python_testing.runner import (
    DurableFunctionTestRunner,
)


# --- Fixtures ----------------------------------------------------------------


def _make_event(overrides=None):
    """Build a minimal valid EventBridge event envelope."""
    detail = {
        "original_message": "msg",
        "iban": "DE89370400440532013000",
        "account_type": "checking",
        "has_holds": "false",
        "suspense_account": "N",
        "head_office_account": "N",
        "tax_category": "standard",
        "billingAmount": "100",
        "transactionAmount": "100",
        "conversionRate": "0",
        "merchantType": "RETAIL",
        "issuingCountryCode": "US",
        "authCode": "123456",
        "acquiringCountryCode": "US",
        "posEntryMode": "chip",
        "systemTraceAuditNumber": "TXN-001",
    }
    if overrides:
        detail.update(overrides)
    return {
        "source": "octank.payments.enrichment",
        "detail-type": "TransactionEnriched",
        "detail": detail,
    }


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch):
    monkeypatch.setenv("EVENT_BUS_NAME", "test-bus")
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-2:123456789012:test-topic")


@pytest.fixture(autouse=True)
def _mock_boto(monkeypatch):
    """Patch the module-level boto3 clients in app so no real calls are made."""
    import app

    mock_events = MagicMock()
    mock_events.put_events.return_value = {"FailedEntryCount": 0, "Entries": [{"EventId": "fake"}]}
    monkeypatch.setattr(app, "events_client", mock_events)

    mock_sns = MagicMock()
    mock_sns.publish.return_value = {"MessageId": "fake"}
    monkeypatch.setattr(app, "sns_client", mock_sns)

    # Re-read env vars since they're captured at import time
    monkeypatch.setattr(app, "EVENT_BUS_NAME", "test-bus")
    monkeypatch.setattr(app, "SNS_TOPIC_ARN", "arn:aws:sns:us-east-2:123456789012:test-topic")

    return {"events": mock_events, "sns": mock_sns}


# --- Happy path --------------------------------------------------------------


class TestHappyPath:
    def test_valid_transaction_approved(self, _mock_boto):
        from app import lambda_handler

        with DurableFunctionTestRunner(handler=lambda_handler) as runner:
            result = runner.run(input=_make_event(), timeout=10)

        assert result.status is InvocationStatus.SUCCEEDED
        ret = json.loads(result.result)
        assert ret["status"] == "approved"
        assert ret["systemTraceAuditNumber"] == "TXN-001"

        # Approval event was published
        _mock_boto["events"].put_events.assert_called()
        last_call = _mock_boto["events"].put_events.call_args
        entry = last_call.kwargs["Entries"][0]
        assert entry["DetailType"] == "TransactionPostingApproved"

    def test_foreign_transaction_emits_event(self, _mock_boto):
        """billingAmount != transactionAmount → ForeignTransactionFound event."""
        from app import lambda_handler

        event = _make_event({"billingAmount": "100", "transactionAmount": "200"})
        with DurableFunctionTestRunner(handler=lambda_handler) as runner:
            result = runner.run(input=event, timeout=10)

        assert result.status is InvocationStatus.SUCCEEDED
        detail_types = [
            call.kwargs["Entries"][0]["DetailType"]
            for call in _mock_boto["events"].put_events.call_args_list
        ]
        assert "ForeignTransactionFound" in detail_types
        assert "TransactionPostingApproved" in detail_types

    def test_conversion_rate_emits_event(self, _mock_boto):
        """conversionRate == '1' → CurrencyConversionTransactionFound event."""
        from app import lambda_handler

        event = _make_event({"conversionRate": "1"})
        with DurableFunctionTestRunner(handler=lambda_handler) as runner:
            result = runner.run(input=event, timeout=10)

        assert result.status is InvocationStatus.SUCCEEDED
        detail_types = [
            call.kwargs["Entries"][0]["DetailType"]
            for call in _mock_boto["events"].put_events.call_args_list
        ]
        assert "CurrencyConversionTransactionFound" in detail_types

    def test_merchant_type_emits_event(self, _mock_boto):
        """merchantType == 'AAFF' → WarningMerchantTypeTransactionFound event."""
        from app import lambda_handler

        event = _make_event({"merchantType": "AAFF"})
        with DurableFunctionTestRunner(handler=lambda_handler) as runner:
            result = runner.run(input=event, timeout=10)

        assert result.status is InvocationStatus.SUCCEEDED
        detail_types = [
            call.kwargs["Entries"][0]["DetailType"]
            for call in _mock_boto["events"].put_events.call_args_list
        ]
        assert "WarningMerchantTypeTransactionFound" in detail_types

    def test_no_rule_events_when_all_pass(self, _mock_boto):
        """When all checks pass, only TransactionPostingApproved is emitted."""
        from app import lambda_handler

        event = _make_event({
            "billingAmount": "100",
            "transactionAmount": "100",
            "conversionRate": "0",
            "merchantType": "RETAIL",
        })
        with DurableFunctionTestRunner(handler=lambda_handler) as runner:
            result = runner.run(input=event, timeout=10)

        assert result.status is InvocationStatus.SUCCEEDED
        assert _mock_boto["events"].put_events.call_count == 1
        entry = _mock_boto["events"].put_events.call_args.kwargs["Entries"][0]
        assert entry["DetailType"] == "TransactionPostingApproved"


# --- Validation failure path -------------------------------------------------


class TestValidationFailure:
    def test_empty_issuing_country_fails(self, _mock_boto):
        from app import lambda_handler

        event = _make_event({"issuingCountryCode": ""})
        with DurableFunctionTestRunner(handler=lambda_handler) as runner:
            result = runner.run(input=event, timeout=10)

        assert result.status is InvocationStatus.SUCCEEDED
        ret = json.loads(result.result)
        assert ret["status"] == "failed"

        # SNS failure published with full event envelope
        _mock_boto["sns"].publish.assert_called_once()
        sns_msg = _mock_boto["sns"].publish.call_args.kwargs["Message"]
        assert "TransactionEnriched" in sns_msg

        # No EventBridge events emitted (no approval, no rule events)
        _mock_boto["events"].put_events.assert_not_called()

    def test_missing_issuing_country_fails(self, _mock_boto):
        from app import lambda_handler

        detail = _make_event()["detail"]
        del detail["issuingCountryCode"]
        event = {"source": "test", "detail-type": "test", "detail": detail}

        with DurableFunctionTestRunner(handler=lambda_handler) as runner:
            result = runner.run(input=event, timeout=10)

        assert result.status is InvocationStatus.SUCCEEDED
        ret = json.loads(result.result)
        assert ret["status"] == "failed"


# --- Misconfiguration --------------------------------------------------------


class TestMisconfiguration:
    def test_missing_event_bus_name_raises(self, monkeypatch):
        import app
        monkeypatch.setattr(app, "EVENT_BUS_NAME", None)
        from app import lambda_handler

        with DurableFunctionTestRunner(handler=lambda_handler) as runner:
            result = runner.run(input=_make_event(), timeout=10)

        assert result.status is InvocationStatus.FAILED

    def test_missing_sns_topic_arn_raises(self, monkeypatch):
        import app
        monkeypatch.setattr(app, "SNS_TOPIC_ARN", None)
        from app import lambda_handler

        with DurableFunctionTestRunner(handler=lambda_handler) as runner:
            result = runner.run(input=_make_event(), timeout=10)

        assert result.status is InvocationStatus.FAILED


# --- Event detail schema -----------------------------------------------------


class TestEventDetailSchema:
    def test_emitted_detail_has_exactly_16_fields(self, _mock_boto):
        from app import lambda_handler, RULE_DETAIL_FIELDS

        with DurableFunctionTestRunner(handler=lambda_handler) as runner:
            runner.run(input=_make_event(), timeout=10)

        entry = _mock_boto["events"].put_events.call_args.kwargs["Entries"][0]
        emitted_detail = json.loads(entry["Detail"])
        assert set(emitted_detail.keys()) == set(RULE_DETAIL_FIELDS)
