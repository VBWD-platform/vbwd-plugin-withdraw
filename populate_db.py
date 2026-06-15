"""Idempotent demo data for the withdraw plugin (S79).

Seeds two demo withdraw requests for the standard test user THROUGH
`WithdrawService.create` (real token debits via the "tokens" balance
source — never raw SQL). The requests stay `pending`: the service is
built with `require_admin_approval=True` regardless of the runtime
config, so no payout call is ever made by the seed.

Skips cleanly (exit 0 with a message) when a prerequisite is missing:
the test user, the withdraw repository provider, the paypal payout
provider, the "tokens" balance source, or a sufficient token balance.

Run inside the api container:

    docker compose exec api python /app/plugins/withdraw/populate_db.py
"""
import os
import sys


DEMO_USER_EMAIL = "test@example.com"
DEMO_PROVIDER = "paypal"
DEMO_DESTINATION = {"email": "demo-payout@example.com"}
SECOND_REQUEST_EXTRA_TOKENS = 50


def _ensure_app():
    """Bootstrap the Flask app so plugin enablement (DI providers,
    balance-source registry) and `db.session` are in place."""
    sys.path.insert(0, "/app")
    os.environ.setdefault("FLASK_ENV", "development")
    from vbwd.app import create_app

    return create_app()


def _skip(reason: str) -> int:
    print(f"withdraw: skipping demo seed — {reason}")
    return 0


def _seed() -> int:
    from flask import current_app

    from plugins.withdraw.withdraw.routes import _resolve_payout_provider
    from plugins.withdraw.withdraw.services.balance_sources import (
        UnknownBalanceSourceError,
        resolve_balance_source,
    )
    from plugins.withdraw.withdraw.services.plugin_config import withdraw_config
    from plugins.withdraw.withdraw.services.withdraw_service import (
        UnknownPayoutProviderError,
        WithdrawService,
    )

    user = current_app.container.user_repository().find_by_email(DEMO_USER_EMAIL)
    if user is None:
        return _skip(f"no user {DEMO_USER_EMAIL}")

    repository_provider = getattr(
        current_app.container, "withdraw_request_repository", None
    )
    if repository_provider is None:
        return _skip("withdraw plugin not enabled (no repository provider)")
    repository = repository_provider()

    if repository.find_by_user_id(user.id):
        print("withdraw: demo requests already exist — nothing to do")
        return 0

    try:
        _resolve_payout_provider(DEMO_PROVIDER)
    except UnknownPayoutProviderError:
        return _skip(f"payout provider '{DEMO_PROVIDER}' not enabled")

    try:
        balance_source = resolve_balance_source("tokens")
    except UnknownBalanceSourceError:
        return _skip("'tokens' balance source not registered")

    config = withdraw_config()
    minimum_tokens = int(config["min_withdraw_tokens"])
    demo_amounts = (minimum_tokens, minimum_tokens + SECOND_REQUEST_EXTRA_TOKENS)

    available_tokens, _ = balance_source.get_balance(user.id)
    if available_tokens < sum(demo_amounts):
        return _skip(
            f"token balance {available_tokens} below the "
            f"{sum(demo_amounts)} the demo requests need"
        )

    service = WithdrawService(
        repository=repository,
        balance_source_resolver=resolve_balance_source,
        payout_provider_resolver=_resolve_payout_provider,
        min_withdraw_tokens=minimum_tokens,
        # Demo rows must stay pending — never short-circuit to a payout,
        # whatever require_admin_approval is set to at runtime.
        require_admin_approval=True,
    )
    for amount in demo_amounts:
        request = service.create(
            user_id=user.id,
            provider_name=DEMO_PROVIDER,
            amount=amount,
            destination=dict(DEMO_DESTINATION),
        )
        print(
            f"withdraw: created demo request {request.id} "
            f"({amount} tokens → {request.payout_amount} {request.currency}, "
            f"status {request.status})"
        )
    return 0


def populate_db() -> int:
    app = _ensure_app()
    with app.app_context():
        return _seed()


if __name__ == "__main__":
    sys.exit(populate_db())
