"""Shared Liskov contract suite for every `PayoutProvider` (S79 Slice 2).

One parametrised module asserts the SAME contract on all four concrete
providers (paypal, stripe, truemoney, promptpay) with their SDK/HTTP
seams mocked:

- the destination schema is a list of {"name","type","label_key"} fields;
- `create_payout` acceptance returns a `PayoutResult` with a non-empty
  `provider_payout_id`;
- a provider-side failure raises the typed `PayoutError` — never another
  exception type, never None;
- `get_payout_status` returns a non-empty string.

Lives in the withdraw plugin because the withdraw plugin is the one
caller that depends on these semantics (D1). Each provider builder uses
`pytest.importorskip`, so the suite degrades to per-provider skips when
a plugin is absent from the checkout (e.g. a standalone plugin CI).
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict
from unittest.mock import MagicMock

import pytest

from vbwd.plugins.base import BasePlugin
from vbwd.plugins.payment_provider import (
    PayoutError,
    PayoutProvider,
    PayoutResult,
)


REFERENCE_ID = "withdraw-req-contract"


@dataclass
class PayoutProviderCase:
    """A concrete provider with its seam mocked, plus arrange hooks."""

    name: str
    plugin: PayoutProvider
    destination: Dict[str, Any]
    currency: str
    arrange_success: Callable[[], None]
    arrange_failure: Callable[[], None]


def _build_paypal_case() -> PayoutProviderCase:
    paypal_module = pytest.importorskip("plugins.paypal")
    from vbwd.sdk.interface import SDKResponse

    adapter = MagicMock()
    plugin = paypal_module.PayPalPlugin()
    plugin._get_adapter = lambda: adapter

    def arrange_success():
        adapter.create_payout_batch.return_value = SDKResponse(
            success=True,
            data={"payout_batch_id": "PB-contract", "batch_status": "PENDING"},
        )
        adapter.get_payout_status.return_value = SDKResponse(
            success=True, data={"batch_status": "SUCCESS"}
        )

    def arrange_failure():
        adapter.create_payout_batch.return_value = SDKResponse(
            success=False, error="DENIED"
        )

    return PayoutProviderCase(
        name="paypal",
        plugin=plugin,
        destination={"email": "payee@example.com"},
        currency="EUR",
        arrange_success=arrange_success,
        arrange_failure=arrange_failure,
    )


def _build_stripe_case() -> PayoutProviderCase:
    stripe_module = pytest.importorskip("plugins.stripe")
    from vbwd.sdk.interface import SDKResponse

    adapter = MagicMock()
    plugin = stripe_module.StripePlugin()
    plugin._get_adapter = lambda: adapter

    def arrange_success():
        adapter.create_transfer.return_value = SDKResponse(
            success=True, data={"transfer_id": "tr_contract", "reversed": False}
        )
        adapter.get_transfer_status.return_value = SDKResponse(
            success=True, data={"transfer_id": "tr_contract", "reversed": False}
        )

    def arrange_failure():
        adapter.create_transfer.return_value = SDKResponse(
            success=False, error="No such destination account"
        )

    return PayoutProviderCase(
        name="stripe",
        plugin=plugin,
        destination={"account_id": "acct_contract"},
        currency="EUR",
        arrange_success=arrange_success,
        arrange_failure=arrange_failure,
    )


def _build_truemoney_case() -> PayoutProviderCase:
    truemoney_module = pytest.importorskip("plugins.truemoney")
    from vbwd.sdk.interface import SDKResponse

    adapter = MagicMock()
    plugin = truemoney_module.TrueMoneyPlugin()
    plugin._get_adapter = lambda: adapter

    def arrange_success():
        adapter.create_wallet_transfer.return_value = SDKResponse(
            success=True,
            data={"transfer_id": "TMN-TR-contract", "status": "PROCESSING"},
        )
        adapter.get_transfer_status.return_value = SDKResponse(
            success=True, data={"status": "SUCCESS"}
        )

    def arrange_failure():
        adapter.create_wallet_transfer.return_value = SDKResponse(
            success=False, error="wallet_not_found"
        )

    return PayoutProviderCase(
        name="truemoney",
        plugin=plugin,
        destination={"msisdn": "+66912345678"},
        currency="THB",
        arrange_success=arrange_success,
        arrange_failure=arrange_failure,
    )


def _build_promptpay_case() -> PayoutProviderCase:
    promptpay_module = pytest.importorskip("plugins.promptpay")
    from plugins.promptpay.promptpay.bank_clients.base import (
        BankFundsTransfer,
        BankTransferError,
    )

    bank_client = MagicMock()
    plugin = promptpay_module.PromptPayPlugin()
    plugin._funds_transfer_client = lambda: bank_client

    def arrange_success():
        bank_client.create_funds_transfer.return_value = BankFundsTransfer(
            bank="kbank", bank_transfer_id="KB-TR-contract", status="processing"
        )
        bank_client.get_transfer_status.return_value = "completed"

    def arrange_failure():
        bank_client.create_funds_transfer.side_effect = BankTransferError(
            "KBank transfer failed"
        )

    return PayoutProviderCase(
        name="promptpay",
        plugin=plugin,
        destination={"promptpay_id": "0812345678"},
        currency="THB",
        arrange_success=arrange_success,
        arrange_failure=arrange_failure,
    )


CASE_BUILDERS = [
    _build_paypal_case,
    _build_stripe_case,
    _build_truemoney_case,
    _build_promptpay_case,
]


@pytest.fixture(
    params=CASE_BUILDERS,
    ids=["paypal", "stripe", "truemoney", "promptpay"],
)
def provider_case(request) -> PayoutProviderCase:
    return request.param()


class TestPayoutProviderContract:
    def test_is_a_discoverable_payout_capable_plugin(self, provider_case):
        assert isinstance(provider_case.plugin, PayoutProvider)
        assert isinstance(provider_case.plugin, BasePlugin)
        assert provider_case.plugin.metadata.name == provider_case.name

    def test_destination_schema_shape(self, provider_case):
        schema = provider_case.plugin.get_payout_destination_schema()
        assert isinstance(schema, list)
        assert schema
        for field in schema:
            assert isinstance(field["name"], str) and field["name"]
            assert isinstance(field["type"], str) and field["type"]
            assert isinstance(field["label_key"], str) and field["label_key"]

    def test_create_payout_acceptance_returns_payout_result(self, provider_case):
        provider_case.arrange_success()
        result = provider_case.plugin.create_payout(
            amount=Decimal("12.34"),
            currency=provider_case.currency,
            destination=provider_case.destination,
            reference_id=REFERENCE_ID,
        )
        assert isinstance(result, PayoutResult)
        assert isinstance(result.provider_payout_id, str)
        assert result.provider_payout_id
        assert isinstance(result.status, str)
        assert result.status

    def test_provider_side_failure_raises_payout_error(self, provider_case):
        provider_case.arrange_failure()
        with pytest.raises(PayoutError):
            provider_case.plugin.create_payout(
                amount=Decimal("12.34"),
                currency=provider_case.currency,
                destination=provider_case.destination,
                reference_id=REFERENCE_ID,
            )

    def test_get_payout_status_returns_non_empty_string(self, provider_case):
        provider_case.arrange_success()
        status = provider_case.plugin.get_payout_status("payout-id-contract")
        assert isinstance(status, str)
        assert status
