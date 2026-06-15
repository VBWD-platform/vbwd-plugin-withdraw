"""Contract-honouring fakes shared by the withdraw unit/integration specs.

Liskov: the fakes obey the exact `PayoutProvider` / `IWithdrawableBalance`
semantics the production implementations honour — `create_payout` returns
a `PayoutResult` or raises the typed `PayoutError`, `debit` raises
`InsufficientBalanceError` instead of silently failing.
"""
from decimal import Decimal
from typing import Any, Dict, List, Tuple
from uuid import UUID

from vbwd.plugins.base import BasePlugin, PluginMetadata
from vbwd.plugins.payment_provider import PayoutError, PayoutProvider, PayoutResult

from plugins.withdraw.withdraw.services.balance_sources import (
    InsufficientBalanceError,
    IWithdrawableBalance,
)


FAKE_PROVIDER_NAME = "fake-pay"

FAKE_DESTINATION_SCHEMA = [
    {"name": "email", "type": "email", "label_key": "withdraw.fake_pay_email"}
]


class RecordingPayoutProvider(PayoutProvider):
    """Accepts every payout and records the call arguments."""

    def __init__(self, payout_status: str = "completed"):
        self.payout_status = payout_status
        self.create_payout_calls: List[Tuple[Decimal, str, Dict[str, Any], str]] = []

    def get_payout_destination_schema(self) -> List[Dict[str, Any]]:
        return FAKE_DESTINATION_SCHEMA

    def create_payout(
        self,
        amount: Decimal,
        currency: str,
        destination: Dict[str, Any],
        reference_id: str,
    ) -> PayoutResult:
        self.create_payout_calls.append((amount, currency, destination, reference_id))
        return PayoutResult(
            provider_payout_id=f"fake-payout-{len(self.create_payout_calls)}",
            status=self.payout_status,
        )

    def get_payout_status(self, provider_payout_id: str) -> str:
        return self.payout_status


class FailingPayoutProvider(PayoutProvider):
    """Rejects every payout with the typed `PayoutError` (Liskov: no
    provider-specific exception leaks to the caller)."""

    def __init__(self):
        self.create_payout_calls: List[Tuple[Decimal, str, Dict[str, Any], str]] = []

    def get_payout_destination_schema(self) -> List[Dict[str, Any]]:
        return FAKE_DESTINATION_SCHEMA

    def create_payout(
        self,
        amount: Decimal,
        currency: str,
        destination: Dict[str, Any],
        reference_id: str,
    ) -> PayoutResult:
        self.create_payout_calls.append((amount, currency, destination, reference_id))
        raise PayoutError("provider rejected the payout")

    def get_payout_status(self, provider_payout_id: str) -> str:
        return "failed"


class FakePayoutPlugin(BasePlugin, RecordingPayoutProvider):
    """A payout-capable plugin as the providers endpoint discovers them:
    a `BasePlugin` that additionally implements `PayoutProvider`."""

    def __init__(self, name: str = FAKE_PROVIDER_NAME):
        BasePlugin.__init__(self)
        RecordingPayoutProvider.__init__(self)
        self._name = name

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name=self._name,
            version="1.0.0",
            author="tests",
            description="Fake payout-capable payment plugin",
        )


class InMemoryWithdrawableBalance(IWithdrawableBalance):
    """In-memory token-style balance source; counts debits/refunds so the
    "exactly once" specs can assert call counts."""

    def __init__(
        self,
        tokens: int,
        token_to_currency_rate: Decimal = Decimal("0.01"),
        currency: str = "EUR",
    ):
        self.tokens = tokens
        self._token_to_currency_rate = token_to_currency_rate
        self._currency = currency
        self.debit_calls: List[Tuple[UUID, int, UUID]] = []
        self.refund_calls: List[Tuple[UUID, int, UUID]] = []

    def get_balance(self, user_id: UUID) -> Tuple[Decimal, str]:
        return Decimal(self.tokens), self._currency

    def debit(
        self, user_id: UUID, amount: int, reference_id: UUID
    ) -> Tuple[Decimal, str]:
        if amount > self.tokens:
            raise InsufficientBalanceError("Insufficient balance")
        self.tokens -= amount
        self.debit_calls.append((user_id, amount, reference_id))
        payout_amount = (self._token_to_currency_rate * amount).quantize(
            Decimal("0.01")
        )
        return payout_amount, self._currency

    def refund(self, user_id: UUID, amount: int, reference_id: UUID) -> None:
        self.tokens += amount
        self.refund_calls.append((user_id, amount, reference_id))
