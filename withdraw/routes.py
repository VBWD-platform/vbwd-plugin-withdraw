"""Flat Blueprint for every withdraw API route (S79).

All routes use absolute `/api/v1/…` paths (public + admin prefixes →
`get_url_prefix()` returns ""). Services are built in factory functions
per request; the repository is resolved THROUGH the DI container
provider registered in `on_enable` (validates the full runtime path).

THIS CONTRACT IS FIXED — the fe-user withdraw plugin is built against
it in parallel.
"""
from decimal import Decimal, InvalidOperation
from typing import Any, List, Optional
from uuid import UUID

from flask import Blueprint, current_app, g, jsonify, request

from vbwd.extensions import db
from vbwd.middleware.auth import require_admin, require_auth
from vbwd.plugins.payment_provider import PayoutProvider

from plugins.withdraw.withdraw.models.withdraw_request import (
    DEFAULT_BALANCE_SOURCE,
)
from plugins.withdraw.withdraw.services.balance_sources import (
    InsufficientBalanceError,
    UnknownBalanceSourceError,
    resolve_balance_source,
)
from plugins.withdraw.withdraw.services.plugin_config import withdraw_config
from plugins.withdraw.withdraw.services.withdraw_service import (
    BelowMinimumWithdrawError,
    InvalidWithdrawStatusError,
    UnknownPayoutProviderError,
    WithdrawRequestNotFoundError,
    WithdrawService,
)


withdraw_bp = Blueprint("withdraw", __name__)

_CREATE_VALIDATION_ERRORS = (
    BelowMinimumWithdrawError,
    InsufficientBalanceError,
    UnknownBalanceSourceError,
    UnknownPayoutProviderError,
)


def _enabled_payout_providers() -> List[PayoutProvider]:
    """Enabled plugins that opted into the payout capability (D1)."""
    plugin_manager = getattr(current_app, "plugin_manager", None)
    if plugin_manager is None:
        return []
    return [
        plugin
        for plugin in plugin_manager.get_enabled_plugins()
        if isinstance(plugin, PayoutProvider)
    ]


def _resolve_payout_provider(provider_name: str) -> PayoutProvider:
    for plugin in _enabled_payout_providers():
        if plugin.metadata.name == provider_name:
            return plugin
    raise UnknownPayoutProviderError(f"Unknown provider: {provider_name}")


def _withdraw_service() -> WithdrawService:
    config = withdraw_config()
    return WithdrawService(
        repository=current_app.container.withdraw_request_repository(),
        balance_source_resolver=resolve_balance_source,
        payout_provider_resolver=_resolve_payout_provider,
        min_withdraw_tokens=int(config["min_withdraw_tokens"]),
        require_admin_approval=bool(config["require_admin_approval"]),
    )


def _caller_user_id() -> UUID:
    """`g.user_id` arrives as a string from the JWT middleware."""
    return UUID(str(g.user_id))


def _parse_request_id(raw_request_id: str) -> Optional[UUID]:
    try:
        return UUID(raw_request_id)
    except ValueError:
        return None


def _parse_payout_amount(raw_payout_amount: Any) -> Optional[Decimal]:
    """A positive `Decimal`, or None when the value is unparseable or not
    strictly positive (booleans included — `Decimal("True")` is invalid)."""
    try:
        payout_amount = Decimal(str(raw_payout_amount))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if payout_amount <= 0:
        return None
    return payout_amount


# ── /api/v1/withdraw/* (auth, self-service) ────────────────────────────────


@withdraw_bp.route("/api/v1/withdraw/providers", methods=["GET"])
@require_auth
def list_payout_providers():
    config = withdraw_config()
    providers = [
        {
            "name": plugin.metadata.name,
            "destination_schema": plugin.get_payout_destination_schema(),
        }
        for plugin in _enabled_payout_providers()
    ]
    return (
        jsonify(
            {
                "providers": providers,
                "config": {
                    "currency": str(config["currency"]),
                    "token_to_currency_rate": float(config["token_to_currency_rate"]),
                    "min_withdraw_tokens": int(config["min_withdraw_tokens"]),
                },
            }
        ),
        200,
    )


@withdraw_bp.route("/api/v1/withdraw", methods=["POST"])
@require_auth
def create_withdraw_request():
    data = request.get_json(silent=True) or {}
    provider_name = data.get("provider")
    amount = data.get("amount")
    destination = data.get("destination")
    balance_source = data.get("balance_source", DEFAULT_BALANCE_SOURCE)

    if not isinstance(provider_name, str) or not provider_name:
        return jsonify({"error": "provider is required"}), 400
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return jsonify({"error": "amount must be a positive integer"}), 400
    if not isinstance(destination, dict) or not destination:
        return jsonify({"error": "destination is required"}), 400
    if not isinstance(balance_source, str) or not balance_source:
        return jsonify({"error": "balance_source must be a string"}), 400

    try:
        withdraw_request = _withdraw_service().create(
            user_id=_caller_user_id(),
            provider_name=provider_name,
            amount=amount,
            destination=destination,
            balance_source=balance_source,
        )
    except _CREATE_VALIDATION_ERRORS as error:
        db.session.rollback()
        return jsonify({"error": str(error)}), 400
    return jsonify({"request": withdraw_request.to_dict()}), 201


@withdraw_bp.route("/api/v1/withdraw/requests", methods=["GET"])
@require_auth
def list_own_withdraw_requests():
    rows = _withdraw_service().list_own(_caller_user_id())
    return jsonify({"requests": [row.to_dict() for row in rows]}), 200


@withdraw_bp.route("/api/v1/withdraw/requests/<request_id>", methods=["GET"])
@require_auth
def get_own_withdraw_request(request_id: str):
    parsed_request_id = _parse_request_id(request_id)
    if parsed_request_id is None:
        return jsonify({"error": "Withdraw request not found"}), 404
    row = _withdraw_service().get_own(parsed_request_id, user_id=_caller_user_id())
    if row is None:
        return jsonify({"error": "Withdraw request not found"}), 404
    return jsonify({"request": row.to_dict()}), 200


# ── /api/v1/admin/withdraw/* (admin approval, API-only in v1) ───────────────


@withdraw_bp.route("/api/v1/admin/withdraw/requests", methods=["GET"])
@require_auth
@require_admin
def admin_list_withdraw_requests():
    status = request.args.get("status") or None
    rows = _withdraw_service().admin_list(status=status)
    return jsonify({"requests": [row.to_dict() for row in rows]}), 200


@withdraw_bp.route("/api/v1/admin/withdraw/requests/<request_id>", methods=["GET"])
@require_auth
@require_admin
def admin_get_withdraw_request(request_id: str):
    parsed_request_id = _parse_request_id(request_id)
    if parsed_request_id is None:
        return jsonify({"error": "Withdraw request not found"}), 404
    try:
        row = _withdraw_service().admin_get(parsed_request_id)
    except WithdrawRequestNotFoundError:
        return jsonify({"error": "Withdraw request not found"}), 404
    return jsonify({"request": row.to_dict()}), 200


@withdraw_bp.route(
    "/api/v1/admin/withdraw/requests/<request_id>/approve", methods=["POST"]
)
@require_auth
@require_admin
def admin_approve_withdraw_request(request_id: str):
    parsed_request_id = _parse_request_id(request_id)
    if parsed_request_id is None:
        return jsonify({"error": "Withdraw request not found"}), 404
    data = request.get_json(silent=True) or {}
    payout_amount_override: Optional[Decimal] = None
    raw_payout_amount = data.get("payout_amount")
    if raw_payout_amount is not None:
        payout_amount_override = _parse_payout_amount(raw_payout_amount)
        if payout_amount_override is None:
            return jsonify({"error": "Invalid payout amount"}), 400
    try:
        row = _withdraw_service().approve(
            parsed_request_id, payout_amount_override=payout_amount_override
        )
    except WithdrawRequestNotFoundError as error:
        return jsonify({"error": str(error)}), 404
    except InvalidWithdrawStatusError as error:
        return jsonify({"error": str(error)}), 409
    except (UnknownPayoutProviderError, UnknownBalanceSourceError) as error:
        return jsonify({"error": str(error)}), 400
    except ValueError:
        return jsonify({"error": "Invalid payout amount"}), 400
    return jsonify({"request": row.to_dict()}), 200


@withdraw_bp.route(
    "/api/v1/admin/withdraw/requests/<request_id>/reject", methods=["POST"]
)
@require_auth
@require_admin
def admin_reject_withdraw_request(request_id: str):
    parsed_request_id = _parse_request_id(request_id)
    if parsed_request_id is None:
        return jsonify({"error": "Withdraw request not found"}), 404
    data = request.get_json(silent=True) or {}
    reason = data.get("reason")
    try:
        row = _withdraw_service().reject(parsed_request_id, reason=reason)
    except WithdrawRequestNotFoundError as error:
        return jsonify({"error": str(error)}), 404
    except InvalidWithdrawStatusError as error:
        return jsonify({"error": str(error)}), 409
    except UnknownBalanceSourceError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"request": row.to_dict()}), 200


@withdraw_bp.route(
    "/api/v1/admin/withdraw/requests/<request_id>/pending", methods=["POST"]
)
@require_auth
@require_admin
def admin_set_withdraw_request_pending(request_id: str):
    parsed_request_id = _parse_request_id(request_id)
    if parsed_request_id is None:
        return jsonify({"error": "Withdraw request not found"}), 404
    try:
        row = _withdraw_service().set_pending(parsed_request_id)
    except WithdrawRequestNotFoundError as error:
        return jsonify({"error": str(error)}), 404
    except InvalidWithdrawStatusError as error:
        return jsonify({"error": str(error)}), 409
    except (InsufficientBalanceError, UnknownBalanceSourceError) as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"request": row.to_dict()}), 200


@withdraw_bp.route("/api/v1/admin/withdraw/requests/<request_id>", methods=["DELETE"])
@require_auth
@require_admin
def admin_delete_withdraw_request(request_id: str):
    parsed_request_id = _parse_request_id(request_id)
    if parsed_request_id is None:
        return jsonify({"error": "Withdraw request not found"}), 404
    try:
        _withdraw_service().delete(parsed_request_id)
    except WithdrawRequestNotFoundError as error:
        return jsonify({"error": str(error)}), 404
    except InvalidWithdrawStatusError as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({"success": True}), 200
