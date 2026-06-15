"""Balance-source seam for withdraw (S79 D3/D4).

`IWithdrawableBalance` is the 3-method port a withdrawable balance must
honour; sources register in a plain dict registry keyed by
`balance_source`. v1 registers only "tokens" — S80 (GHRM marketplace
"My money") plugs its earnings ledger into exactly this registry.

Liskov: every source raises `InsufficientBalanceError` (never a silent
failure) and `debit` returns the converted payout value of the debited
amount, so `WithdrawService` stays source-agnostic.
"""
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Callable, Dict, Tuple
from uuid import UUID

from flask import current_app

from vbwd.models.enums import TokenTransactionType


TWO_DECIMAL_PLACES = Decimal("0.01")


class InsufficientBalanceError(Exception):
    """The user's balance does not cover the requested amount."""


class UnknownBalanceSourceError(Exception):
    """No source is registered under the requested `balance_source` key."""


class IWithdrawableBalance(ABC):
    """A balance a user can withdraw money from (D3)."""

    @abstractmethod
    def get_balance(self, user_id: UUID) -> Tuple[Decimal, str]:
        """Return (balance in source units, payout currency)."""
        pass

    @abstractmethod
    def debit(
        self, user_id: UUID, amount: int, reference_id: UUID
    ) -> Tuple[Decimal, str]:
        """Debit `amount` source units, audit-tagged with `reference_id`
        (the withdraw request id).

        Returns:
            (payout value of the debited amount, currency) — the
            source owns the source-unit → money conversion (D4).

        Raises:
            InsufficientBalanceError: If the balance does not cover it.
        """
        pass

    @abstractmethod
    def refund(self, user_id: UUID, amount: int, reference_id: UUID) -> None:
        """Credit `amount` source units back (failed/rejected request)."""
        pass


class TokenWithdrawableBalance(IWithdrawableBalance):
    """Token-backed source: debit/refund go through the core
    `TokenService` with the `WITHDRAW` ledger vocabulary; conversion is
    `token_to_currency_rate` × tokens (D4)."""

    def __init__(
        self,
        token_service,
        token_to_currency_rate: Decimal,
        currency: str,
    ):
        self._token_service = token_service
        self._token_to_currency_rate = token_to_currency_rate
        self._currency = currency

    def get_balance(self, user_id: UUID) -> Tuple[Decimal, str]:
        return Decimal(self._token_service.get_balance(user_id)), self._currency

    def debit(
        self, user_id: UUID, amount: int, reference_id: UUID
    ) -> Tuple[Decimal, str]:
        try:
            self._token_service.debit_tokens(
                user_id=user_id,
                amount=amount,
                transaction_type=TokenTransactionType.WITHDRAW,
                reference_id=reference_id,
                description="Withdraw request",
            )
        except ValueError as error:
            raise InsufficientBalanceError(str(error)) from error
        return self._convert(amount), self._currency

    def refund(self, user_id: UUID, amount: int, reference_id: UUID) -> None:
        self._token_service.credit_tokens(
            user_id=user_id,
            amount=amount,
            transaction_type=TokenTransactionType.WITHDRAW,
            reference_id=reference_id,
            description="Withdraw refund",
        )

    def _convert(self, amount: int) -> Decimal:
        return (self._token_to_currency_rate * amount).quantize(TWO_DECIMAL_PLACES)


# Plain dict registry keyed by `balance_source` (D3). Factories rather
# than instances so each request resolves the source with the current
# config / DI container state.
_balance_source_factories: Dict[str, Callable[[], IWithdrawableBalance]] = {}


def register_balance_source(
    name: str, factory: Callable[[], IWithdrawableBalance]
) -> None:
    """Register (or replace) the factory for a balance source."""
    _balance_source_factories[name] = factory


def unregister_balance_source(name: str) -> None:
    """Remove a source; no-op if absent (safe to call from on_disable twice)."""
    _balance_source_factories.pop(name, None)


def resolve_balance_source(name: str) -> IWithdrawableBalance:
    """Build the source registered under `name`.

    Raises:
        UnknownBalanceSourceError: If nothing is registered under `name`.
    """
    factory = _balance_source_factories.get(name)
    if factory is None:
        raise UnknownBalanceSourceError(f"Unknown balance source: {name}")
    return factory()


def create_token_withdrawable_balance() -> TokenWithdrawableBalance:
    """Factory for the "tokens" source: core `TokenService` from the DI
    container, rate/currency read fresh from the plugin config."""
    from plugins.withdraw.withdraw.services.plugin_config import withdraw_config

    config = withdraw_config()
    return TokenWithdrawableBalance(
        token_service=current_app.container.token_service(),
        token_to_currency_rate=Decimal(str(config["token_to_currency_rate"])),
        currency=str(config["currency"]),
    )
