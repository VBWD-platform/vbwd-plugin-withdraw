"""`WithdrawService` orchestration specs (S79 D6) — mocked collaborators.

Pin the money-safety invariants: validation failures debit NOTHING;
the happy path debits exactly once with `reference_id` = the request id;
approve forwards the converted amount/currency/destination; `PayoutError`
and reject each refund exactly once; `require_admin_approval=false`
short-circuits straight to payout.
"""
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from plugins.withdraw.withdraw.models.withdraw_request import (
    STATUS_APPROVED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_REJECTED,
)
from plugins.withdraw.withdraw.services.balance_sources import (
    InsufficientBalanceError,
    UnknownBalanceSourceError,
)
from plugins.withdraw.withdraw.services.withdraw_service import (
    BelowMinimumWithdrawError,
    InvalidWithdrawStatusError,
    UnknownPayoutProviderError,
    WithdrawRequestNotFoundError,
    WithdrawService,
)
from plugins.withdraw.tests.unit.fakes import (
    FAKE_PROVIDER_NAME,
    FailingPayoutProvider,
    InMemoryWithdrawableBalance,
    RecordingPayoutProvider,
)


DESTINATION = {"email": "user@example.com"}
MIN_WITHDRAW_TOKENS = 100


def _resolver_for(provider):
    def resolve(provider_name: str):
        if provider_name == FAKE_PROVIDER_NAME:
            return provider
        raise UnknownPayoutProviderError(f"Unknown provider: {provider_name}")

    return resolve


def _source_resolver_for(source):
    def resolve(source_name: str):
        if source_name == "tokens":
            return source
        raise UnknownBalanceSourceError(f"Unknown balance source: {source_name}")

    return resolve


def _service(
    provider,
    source,
    repository=None,
    require_admin_approval=True,
):
    return WithdrawService(
        repository=repository if repository is not None else MagicMock(),
        balance_source_resolver=_source_resolver_for(source),
        payout_provider_resolver=_resolver_for(provider),
        min_withdraw_tokens=MIN_WITHDRAW_TOKENS,
        require_admin_approval=require_admin_approval,
    )


class TestCreateValidation:
    def test_below_minimum_is_rejected_with_no_debit(self):
        source = InMemoryWithdrawableBalance(tokens=500)
        repository = MagicMock()
        service = _service(RecordingPayoutProvider(), source, repository)

        with pytest.raises(BelowMinimumWithdrawError):
            service.create(uuid4(), FAKE_PROVIDER_NAME, 99, DESTINATION)

        assert source.debit_calls == []
        repository.create.assert_not_called()

    def test_over_balance_is_rejected_with_no_debit(self):
        source = InMemoryWithdrawableBalance(tokens=150)
        repository = MagicMock()
        service = _service(RecordingPayoutProvider(), source, repository)

        with pytest.raises(InsufficientBalanceError):
            service.create(uuid4(), FAKE_PROVIDER_NAME, 200, DESTINATION)

        assert source.debit_calls == []
        repository.create.assert_not_called()

    def test_unknown_provider_is_rejected_with_no_debit(self):
        source = InMemoryWithdrawableBalance(tokens=500)
        service = _service(RecordingPayoutProvider(), source)

        with pytest.raises(UnknownPayoutProviderError):
            service.create(uuid4(), "no-such-provider", 150, DESTINATION)

        assert source.debit_calls == []

    def test_unknown_balance_source_is_rejected_with_no_debit(self):
        source = InMemoryWithdrawableBalance(tokens=500)
        service = _service(RecordingPayoutProvider(), source)

        with pytest.raises(UnknownBalanceSourceError):
            service.create(
                uuid4(), FAKE_PROVIDER_NAME, 150, DESTINATION, "no-such-source"
            )

        assert source.debit_calls == []


class TestCreateHappyPath:
    def test_debits_exactly_once_with_request_id_as_reference(self):
        source = InMemoryWithdrawableBalance(tokens=500)
        repository = MagicMock()
        user_id = uuid4()
        service = _service(RecordingPayoutProvider(), source, repository)

        request = service.create(user_id, FAKE_PROVIDER_NAME, 150, DESTINATION)

        assert source.debit_calls == [(user_id, 150, request.id)]
        repository.create.assert_called_once_with(request)

    def test_request_row_carries_converted_payout_and_pending_status(self):
        source = InMemoryWithdrawableBalance(tokens=500)
        provider = RecordingPayoutProvider()
        service = _service(provider, source)

        request = service.create(uuid4(), FAKE_PROVIDER_NAME, 150, DESTINATION)

        assert request.status == STATUS_PENDING
        assert request.amount == 150
        assert request.payout_amount == Decimal("1.50")
        assert request.currency == "EUR"
        assert request.provider == FAKE_PROVIDER_NAME
        assert request.balance_source == "tokens"
        assert request.destination == DESTINATION
        # admin approval is required → no payout yet
        assert provider.create_payout_calls == []

    def test_no_admin_approval_short_circuits_straight_to_payout(self):
        source = InMemoryWithdrawableBalance(tokens=500)
        provider = RecordingPayoutProvider(payout_status="completed")
        service = _service(provider, source, require_admin_approval=False)

        request = service.create(uuid4(), FAKE_PROVIDER_NAME, 150, DESTINATION)

        assert len(provider.create_payout_calls) == 1
        assert request.status == STATUS_COMPLETED
        assert request.provider_payout_id == "fake-payout-1"


class TestApprove:
    def _pending_request(self, service, source, provider, user_id):
        return service.create(user_id, FAKE_PROVIDER_NAME, 150, DESTINATION)

    def test_passes_converted_amount_currency_destination_to_the_provider(self):
        source = InMemoryWithdrawableBalance(tokens=500)
        provider = RecordingPayoutProvider(payout_status="completed")
        repository = MagicMock()
        service = _service(provider, source, repository)
        request = service.create(uuid4(), FAKE_PROVIDER_NAME, 150, DESTINATION)
        repository.find_by_id.return_value = request

        service.approve(request.id)

        assert provider.create_payout_calls == [
            (Decimal("1.50"), "EUR", DESTINATION, str(request.id))
        ]
        assert request.status == STATUS_COMPLETED
        assert request.provider_payout_id == "fake-payout-1"

    def test_provider_accepting_without_completion_leaves_processing(self):
        source = InMemoryWithdrawableBalance(tokens=500)
        provider = RecordingPayoutProvider(payout_status="processing")
        repository = MagicMock()
        service = _service(provider, source, repository)
        request = service.create(uuid4(), FAKE_PROVIDER_NAME, 150, DESTINATION)
        repository.find_by_id.return_value = request

        service.approve(request.id)

        assert request.status == STATUS_PROCESSING

    def test_payout_error_marks_failed_and_refunds_exactly_once(self):
        source = InMemoryWithdrawableBalance(tokens=500)
        provider = FailingPayoutProvider()
        repository = MagicMock()
        user_id = uuid4()
        service = _service(provider, source, repository)
        request = service.create(user_id, FAKE_PROVIDER_NAME, 150, DESTINATION)
        repository.find_by_id.return_value = request

        service.approve(request.id)

        assert request.status == STATUS_FAILED
        assert "rejected" in request.error
        assert source.refund_calls == [(user_id, 150, request.id)]

    def test_approve_unknown_request_raises_not_found(self):
        repository = MagicMock()
        repository.find_by_id.return_value = None
        service = _service(
            RecordingPayoutProvider(),
            InMemoryWithdrawableBalance(tokens=500),
            repository,
        )

        with pytest.raises(WithdrawRequestNotFoundError):
            service.approve(uuid4())

    def test_approve_non_pending_request_is_refused_without_payout_or_refund(self):
        source = InMemoryWithdrawableBalance(tokens=500)
        provider = RecordingPayoutProvider()
        repository = MagicMock()
        service = _service(provider, source, repository)
        request = service.create(uuid4(), FAKE_PROVIDER_NAME, 150, DESTINATION)
        request.status = STATUS_APPROVED
        repository.find_by_id.return_value = request

        with pytest.raises(InvalidWithdrawStatusError):
            service.approve(request.id)

        assert provider.create_payout_calls == []
        assert source.refund_calls == []


class TestReject:
    def test_reject_refunds_exactly_once_and_records_the_reason(self):
        source = InMemoryWithdrawableBalance(tokens=500)
        repository = MagicMock()
        user_id = uuid4()
        service = _service(RecordingPayoutProvider(), source, repository)
        request = service.create(user_id, FAKE_PROVIDER_NAME, 150, DESTINATION)
        repository.find_by_id.return_value = request

        service.reject(request.id, reason="suspicious destination")

        assert request.status == STATUS_REJECTED
        assert request.error == "suspicious destination"
        assert source.refund_calls == [(user_id, 150, request.id)]

    def test_reject_non_pending_request_is_refused_without_refund(self):
        source = InMemoryWithdrawableBalance(tokens=500)
        repository = MagicMock()
        service = _service(RecordingPayoutProvider(), source, repository)
        request = service.create(uuid4(), FAKE_PROVIDER_NAME, 150, DESTINATION)
        request.status = STATUS_COMPLETED
        repository.find_by_id.return_value = request

        with pytest.raises(InvalidWithdrawStatusError):
            service.reject(request.id)

        assert source.refund_calls == []


class TestReads:
    def test_get_own_returns_none_for_a_foreign_request(self):
        repository = MagicMock()
        foreign_request = MagicMock()
        foreign_request.user_id = uuid4()
        repository.find_by_id.return_value = foreign_request
        service = _service(
            RecordingPayoutProvider(),
            InMemoryWithdrawableBalance(tokens=500),
            repository,
        )

        assert service.get_own(uuid4(), user_id=uuid4()) is None

    def test_list_own_and_admin_list_delegate_to_the_repository(self):
        repository = MagicMock()
        service = _service(
            RecordingPayoutProvider(),
            InMemoryWithdrawableBalance(tokens=500),
            repository,
        )
        user_id = uuid4()

        service.list_own(user_id)
        repository.find_by_user_id.assert_called_once_with(user_id)

        service.admin_list(status=STATUS_PENDING)
        repository.find_all.assert_called_once_with(status=STATUS_PENDING)
