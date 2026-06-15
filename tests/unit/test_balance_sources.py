"""`IWithdrawableBalance` seam + token-backed source (S79 D3/D4).

`TokenWithdrawableBalance` routes every debit/refund through the core
`TokenService` (DI'd in — never raw SQL) with the `WITHDRAW` ledger
vocabulary and converts source units to money at
`token_to_currency_rate` × tokens. The registry is a plain dict keyed
by `balance_source` so S80 can plug an earnings source in without
touching this plugin.
"""
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from vbwd.models.enums import TokenTransactionType

from plugins.withdraw.withdraw.services.balance_sources import (
    InsufficientBalanceError,
    IWithdrawableBalance,
    TokenWithdrawableBalance,
    UnknownBalanceSourceError,
    register_balance_source,
    resolve_balance_source,
    unregister_balance_source,
)


class TestTokenWithdrawableBalance:
    def _source(self, token_service) -> TokenWithdrawableBalance:
        return TokenWithdrawableBalance(
            token_service=token_service,
            token_to_currency_rate=Decimal("0.01"),
            currency="EUR",
        )

    def test_is_a_withdrawable_balance(self):
        assert issubclass(TokenWithdrawableBalance, IWithdrawableBalance)

    def test_get_balance_returns_token_amount_and_payout_currency(self):
        token_service = MagicMock()
        token_service.get_balance.return_value = 250
        user_id = uuid4()

        balance_amount, currency = self._source(token_service).get_balance(user_id)

        assert balance_amount == Decimal("250")
        assert currency == "EUR"
        token_service.get_balance.assert_called_once_with(user_id)

    def test_debit_goes_through_core_token_service_with_withdraw_type(self):
        token_service = MagicMock()
        user_id = uuid4()
        reference_id = uuid4()

        self._source(token_service).debit(user_id, 150, reference_id)

        token_service.debit_tokens.assert_called_once()
        call_kwargs = token_service.debit_tokens.call_args.kwargs
        assert call_kwargs["user_id"] == user_id
        assert call_kwargs["amount"] == 150
        assert call_kwargs["transaction_type"] is TokenTransactionType.WITHDRAW
        assert call_kwargs["reference_id"] == reference_id

    def test_debit_returns_payout_value_converted_at_the_config_rate(self):
        token_service = MagicMock()

        payout_amount, currency = self._source(token_service).debit(
            uuid4(), 150, uuid4()
        )

        assert payout_amount == Decimal("1.50")
        assert currency == "EUR"

    def test_debit_insufficient_balance_raises_the_typed_error(self):
        token_service = MagicMock()
        token_service.debit_tokens.side_effect = ValueError(
            "Insufficient token balance"
        )

        with pytest.raises(InsufficientBalanceError):
            self._source(token_service).debit(uuid4(), 150, uuid4())

    def test_refund_credits_through_core_token_service_with_withdraw_type(self):
        token_service = MagicMock()
        user_id = uuid4()
        reference_id = uuid4()

        self._source(token_service).refund(user_id, 150, reference_id)

        token_service.credit_tokens.assert_called_once()
        call_kwargs = token_service.credit_tokens.call_args.kwargs
        assert call_kwargs["user_id"] == user_id
        assert call_kwargs["amount"] == 150
        assert call_kwargs["transaction_type"] is TokenTransactionType.WITHDRAW
        assert call_kwargs["reference_id"] == reference_id


class TestBalanceSourceRegistry:
    def test_resolve_returns_an_instance_built_by_the_registered_factory(self):
        source = MagicMock(spec=IWithdrawableBalance)
        register_balance_source("spec-source", lambda: source)
        try:
            assert resolve_balance_source("spec-source") is source
        finally:
            unregister_balance_source("spec-source")

    def test_unknown_source_raises_the_typed_error(self):
        with pytest.raises(UnknownBalanceSourceError):
            resolve_balance_source("no-such-source")

    def test_unregister_removes_the_source_and_is_idempotent(self):
        register_balance_source("spec-source", MagicMock)
        unregister_balance_source("spec-source")
        unregister_balance_source("spec-source")
        with pytest.raises(UnknownBalanceSourceError):
            resolve_balance_source("spec-source")
