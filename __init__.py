"""Withdraw plugin — token balance → money payout via payment plugins (S79).

Owns the `WithdrawRequest` model, the orchestration service and the
public + admin API. Core gains only the `PayoutProvider` contract and
the `WITHDRAW` ledger vocabulary (D1/D5); everything gnostic lives
here (D2). Balance sources plug into the `balance_sources` registry —
v1 registers only "tokens" (D3); S80 plugs its earnings ledger in.
"""
from typing import Any, Dict, Optional, TYPE_CHECKING

from flask import current_app

from vbwd.plugins.base import BasePlugin, PluginMetadata

if TYPE_CHECKING:
    from flask import Blueprint


DEFAULT_CONFIG: Dict[str, Any] = {
    "debug_mode": False,
    "token_to_currency_rate": 0.01,
    "currency": "EUR",
    "min_withdraw_tokens": 100,
    "require_admin_approval": True,
}

_REPOSITORY_PROVIDER_NAME = "withdraw_request_repository"
_TOKENS_BALANCE_SOURCE = "tokens"


class WithdrawPlugin(BasePlugin):
    """Class MUST be defined in __init__.py (not re-exported) due to
    discovery check obj.__module__ != full_module in manager.py."""

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="withdraw",
            version="26.6",
            author="VBWD Team",
            description=(
                "Withdraw a token balance as money via payout-capable "
                "payment providers, with admin approval and refund on "
                "failure/rejection"
            ),
        )

    def initialize(self, config: Optional[Dict[str, Any]] = None) -> None:
        merged = {**DEFAULT_CONFIG}
        if config:
            merged.update(config)
        super().initialize(merged)

    def get_blueprint(self) -> Optional["Blueprint"]:
        from plugins.withdraw.withdraw.routes import withdraw_bp

        return withdraw_bp

    def get_url_prefix(self) -> Optional[str]:
        # routes use absolute /api/v1/withdraw/* + /api/v1/admin/withdraw/*
        return ""

    def on_enable(self) -> None:
        from vbwd.plugins.di_helpers import register_repositories
        from plugins.withdraw.withdraw.repositories.withdraw_request_repository import (
            WithdrawRequestRepository,
        )
        from plugins.withdraw.withdraw.services.balance_sources import (
            create_token_withdrawable_balance,
            register_balance_source,
        )

        container = getattr(current_app, "container", None)
        if container is not None:
            register_repositories(
                container,
                {_REPOSITORY_PROVIDER_NAME: WithdrawRequestRepository},
            )
        register_balance_source(
            _TOKENS_BALANCE_SOURCE, create_token_withdrawable_balance
        )

    def on_disable(self) -> None:
        from vbwd.plugins.di_helpers import unregister_repositories
        from plugins.withdraw.withdraw.services.balance_sources import (
            unregister_balance_source,
        )

        container = getattr(current_app, "container", None)
        if container is not None:
            unregister_repositories(container, [_REPOSITORY_PROVIDER_NAME])
        unregister_balance_source(_TOKENS_BALANCE_SOURCE)
