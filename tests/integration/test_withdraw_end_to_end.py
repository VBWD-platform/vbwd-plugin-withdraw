"""End-to-end withdraw flows against real PostgreSQL (S79).

request→approve→completed with a mock provider, asserting the token
balance and the WITHDRAW ledger rows; plus the reject→refund path.
Components are the real production ones (repository, token-backed
source, core TokenService through the DI container) — only the payout
provider is the contract-honouring fake (Liskov: same semantics as the
Slice 2 implementations).
"""
from decimal import Decimal
from uuid import uuid4

import pytest

from plugins.withdraw.withdraw.models.withdraw_request import (
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_REJECTED,
)
from plugins.withdraw.withdraw.repositories.withdraw_request_repository import (
    WithdrawRequestRepository,
)
from plugins.withdraw.withdraw.services.balance_sources import (
    TokenWithdrawableBalance,
    UnknownBalanceSourceError,
)
from plugins.withdraw.withdraw.services.withdraw_service import (
    UnknownPayoutProviderError,
    WithdrawService,
)
from plugins.withdraw.tests.unit.fakes import (
    FAKE_PROVIDER_NAME,
    RecordingPayoutProvider,
)


TOKEN_TO_CURRENCY_RATE = Decimal("0.01")
CURRENCY = "EUR"
MIN_WITHDRAW_TOKENS = 100
INITIAL_TOKENS = 500


@pytest.fixture
def user(app):
    from vbwd.extensions import db
    from vbwd.models.user import User

    row = User(
        id=uuid4(),
        email=f"withdraw-e2e-{uuid4().hex[:8]}@example.com",
        password_hash="x",
    )
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def token_service(app, user):
    from vbwd.models.enums import TokenTransactionType

    service = app.container.token_service()
    service.credit_tokens(
        user_id=user.id,
        amount=INITIAL_TOKENS,
        transaction_type=TokenTransactionType.BONUS,
        description="withdraw e2e seed",
    )
    return service


def _service(app, token_service, provider):
    from vbwd.extensions import db

    source = TokenWithdrawableBalance(
        token_service=token_service,
        token_to_currency_rate=TOKEN_TO_CURRENCY_RATE,
        currency=CURRENCY,
    )

    def resolve_source(source_name):
        if source_name == "tokens":
            return source
        raise UnknownBalanceSourceError(source_name)

    def resolve_provider(provider_name):
        if provider_name == FAKE_PROVIDER_NAME:
            return provider
        raise UnknownPayoutProviderError(provider_name)

    return WithdrawService(
        repository=WithdrawRequestRepository(db.session),
        balance_source_resolver=resolve_source,
        payout_provider_resolver=resolve_provider,
        min_withdraw_tokens=MIN_WITHDRAW_TOKENS,
        require_admin_approval=True,
    )


def _withdraw_ledger_rows(user_id):
    from vbwd.extensions import db
    from vbwd.models.enums import TokenTransactionType
    from vbwd.models.user_token_balance import TokenTransaction

    return (
        db.session.query(TokenTransaction)
        .filter(
            TokenTransaction.user_id == user_id,
            TokenTransaction.transaction_type == TokenTransactionType.WITHDRAW,
        )
        .order_by(TokenTransaction.created_at)
        .all()
    )


@pytest.mark.integration
def test_request_approve_completed_end_to_end(app, user, token_service):
    provider = RecordingPayoutProvider(payout_status="completed")
    service = _service(app, token_service, provider)
    destination = {"email": "payee@example.com"}

    request = service.create(user.id, FAKE_PROVIDER_NAME, 200, destination)

    assert request.status == STATUS_PENDING
    assert token_service.get_balance(user.id) == INITIAL_TOKENS - 200
    debit_rows = _withdraw_ledger_rows(user.id)
    assert len(debit_rows) == 1
    assert debit_rows[0].amount == -200
    assert debit_rows[0].reference_id == request.id

    approved = service.approve(request.id)

    assert approved.status == STATUS_COMPLETED
    assert approved.provider_payout_id == "fake-payout-1"
    assert provider.create_payout_calls == [
        (Decimal("2.00"), CURRENCY, destination, str(request.id))
    ]
    # the persisted row reflects the terminal state
    from vbwd.extensions import db

    reloaded = WithdrawRequestRepository(db.session).find_by_id(request.id)
    assert reloaded.status == STATUS_COMPLETED
    assert reloaded.payout_amount == Decimal("2.00")


@pytest.mark.integration
def test_reject_refunds_the_tokens(app, user, token_service):
    provider = RecordingPayoutProvider()
    service = _service(app, token_service, provider)

    request = service.create(
        user.id, FAKE_PROVIDER_NAME, 150, {"email": "payee@example.com"}
    )
    assert token_service.get_balance(user.id) == INITIAL_TOKENS - 150

    rejected = service.reject(request.id, reason="not today")

    assert rejected.status == STATUS_REJECTED
    assert token_service.get_balance(user.id) == INITIAL_TOKENS
    ledger_rows = _withdraw_ledger_rows(user.id)
    assert [row.amount for row in ledger_rows] == [-150, 150]
    assert all(row.reference_id == request.id for row in ledger_rows)
    assert provider.create_payout_calls == []


@pytest.mark.integration
def test_own_requests_listed_newest_first(app, user, token_service):
    from vbwd.extensions import db

    service = _service(app, token_service, RecordingPayoutProvider())
    first = service.create(
        user.id, FAKE_PROVIDER_NAME, 100, {"email": "payee@example.com"}
    )
    second = service.create(
        user.id, FAKE_PROVIDER_NAME, 100, {"email": "payee@example.com"}
    )

    listed = WithdrawRequestRepository(db.session).find_by_user_id(user.id)

    assert [row.id for row in listed][:2] == [second.id, first.id]
