"""WithdrawService — orchestrates request → approval → payout (S79 D6).

Money-safety invariants:
- validation failures (below minimum / insufficient balance / unknown
  provider or source) debit NOTHING;
- the happy path debits exactly once, audit-tagged with the request id;
- `PayoutError` and reject each refund exactly once (only a `pending`
  request can be approved/rejected, so the transition — and with it the
  refund — can only happen once).

Collaborators arrive as narrow callables/ports (DI): the repository,
a `balance_source` resolver and a payout-provider resolver — the
service knows neither the plugin manager nor any concrete provider.
"""
from typing import Any, Callable, Dict, List, Optional
from uuid import UUID, uuid4

from vbwd.plugins.payment_provider import PayoutError, PayoutProvider

from plugins.withdraw.withdraw.models.withdraw_request import (
    DEFAULT_BALANCE_SOURCE,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_REJECTED,
    WithdrawRequest,
)
from plugins.withdraw.withdraw.services.balance_sources import (
    InsufficientBalanceError,
    IWithdrawableBalance,
)


PROVIDER_STATUS_COMPLETED = "completed"


class BelowMinimumWithdrawError(Exception):
    """Requested amount is below the configured minimum."""


class UnknownPayoutProviderError(Exception):
    """No enabled payout-capable provider matches the requested name."""


class WithdrawRequestNotFoundError(Exception):
    """No withdraw request exists under the given id."""


class InvalidWithdrawStatusError(Exception):
    """The request is not in a status that allows the operation."""


class WithdrawService:
    """Validate → debit once → request row; approve → payout; failure → refund."""

    def __init__(
        self,
        repository,
        balance_source_resolver: Callable[[str], IWithdrawableBalance],
        payout_provider_resolver: Callable[[str], PayoutProvider],
        min_withdraw_tokens: int,
        require_admin_approval: bool,
    ):
        self._repository = repository
        self._balance_source_resolver = balance_source_resolver
        self._payout_provider_resolver = payout_provider_resolver
        self._min_withdraw_tokens = min_withdraw_tokens
        self._require_admin_approval = require_admin_approval

    def create(
        self,
        user_id: UUID,
        provider_name: str,
        amount: int,
        destination: Dict[str, Any],
        balance_source: str = DEFAULT_BALANCE_SOURCE,
    ) -> WithdrawRequest:
        """Validate the request, debit the balance exactly once and persist
        the row (status `pending`, or straight to payout when admin
        approval is disabled).

        Raises:
            UnknownPayoutProviderError, UnknownBalanceSourceError,
            BelowMinimumWithdrawError, InsufficientBalanceError
        """
        provider = self._payout_provider_resolver(provider_name)
        source = self._balance_source_resolver(balance_source)

        if amount < self._min_withdraw_tokens:
            raise BelowMinimumWithdrawError(
                f"Minimum withdraw amount is {self._min_withdraw_tokens}"
            )
        balance_amount, _ = source.get_balance(user_id)
        if balance_amount < amount:
            raise InsufficientBalanceError("Insufficient balance")

        request = WithdrawRequest(
            id=uuid4(),
            user_id=user_id,
            balance_source=balance_source,
            amount=amount,
            provider=provider_name,
            destination=destination,
            status=STATUS_PENDING,
        )
        # Debit BEFORE persisting: a pending row must always be backed by
        # a real debit, otherwise an admin could approve money that was
        # never taken from the balance.
        payout_amount, currency = source.debit(
            user_id, amount, reference_id=request.id
        )
        request.payout_amount = payout_amount
        request.currency = currency
        self._repository.create(request)

        if not self._require_admin_approval:
            self._execute_payout(request, provider, source)
        return request

    def approve(self, request_id: UUID) -> WithdrawRequest:
        """Send the payout for a pending request; `PayoutError` marks it
        failed and refunds exactly once."""
        request = self._find_or_raise(request_id)
        self._require_status(request, STATUS_PENDING)
        provider = self._payout_provider_resolver(request.provider)
        source = self._balance_source_resolver(request.balance_source)
        self._execute_payout(request, provider, source)
        return request

    def reject(
        self, request_id: UUID, reason: Optional[str] = None
    ) -> WithdrawRequest:
        """Reject a pending request and refund the debited amount."""
        request = self._find_or_raise(request_id)
        self._require_status(request, STATUS_PENDING)
        source = self._balance_source_resolver(request.balance_source)
        request.status = STATUS_REJECTED
        request.error = reason
        source.refund(request.user_id, request.amount, reference_id=request.id)
        self._repository.save(request)
        return request

    def list_own(self, user_id: UUID) -> List[WithdrawRequest]:
        return self._repository.find_by_user_id(user_id)

    def get_own(
        self, request_id: UUID, user_id: UUID
    ) -> Optional[WithdrawRequest]:
        """The request, or None when it doesn't exist or belongs to
        someone else (owner-only read — callers answer 404 either way)."""
        request = self._repository.find_by_id(request_id)
        if request is None or request.user_id != user_id:
            return None
        return request

    def admin_list(self, status: Optional[str] = None) -> List[WithdrawRequest]:
        return self._repository.find_all(status=status)

    def _execute_payout(
        self,
        request: WithdrawRequest,
        provider: PayoutProvider,
        source: IWithdrawableBalance,
    ) -> None:
        try:
            result = provider.create_payout(
                amount=request.payout_amount,
                currency=request.currency,
                destination=request.destination,
                reference_id=str(request.id),
            )
        except PayoutError as error:
            request.status = STATUS_FAILED
            request.error = str(error)
            source.refund(request.user_id, request.amount, reference_id=request.id)
        else:
            request.provider_payout_id = result.provider_payout_id
            request.status = (
                STATUS_COMPLETED
                if result.status == PROVIDER_STATUS_COMPLETED
                else STATUS_PROCESSING
            )
        self._repository.save(request)

    def _find_or_raise(self, request_id: UUID) -> WithdrawRequest:
        request = self._repository.find_by_id(request_id)
        if request is None:
            raise WithdrawRequestNotFoundError(
                f"Withdraw request {request_id} not found"
            )
        return request

    @staticmethod
    def _require_status(request: WithdrawRequest, expected_status: str) -> None:
        if request.status != expected_status:
            raise InvalidWithdrawStatusError(
                f"Withdraw request is {request.status}, expected {expected_status}"
            )
