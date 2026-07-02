"""Route-level specs for the withdraw API (S79) — fixed contract.

The fe-user withdraw plugin is built against exactly these shapes in
parallel. Auth is stubbed at the middleware boundary (house pattern);
the providers endpoint discovers payout-capable plugins via a patched
`plugin_manager` so no real provider plugin is needed (they are
Slice 2).
"""
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from vbwd.plugins.base import BasePlugin

from plugins.withdraw.tests.unit.fakes import (
    FAKE_DESTINATION_SCHEMA,
    FAKE_PROVIDER_NAME,
    FakePayoutPlugin,
)
from plugins.withdraw.withdraw.services.balance_sources import (
    InsufficientBalanceError,
    UnknownBalanceSourceError,
)
from plugins.withdraw.withdraw.services.withdraw_service import (
    InvalidWithdrawStatusError,
    WithdrawRequestNotFoundError,
)


PROVIDERS_PATH = "/api/v1/withdraw/providers"
CREATE_PATH = "/api/v1/withdraw"
REQUESTS_PATH = "/api/v1/withdraw/requests"
ADMIN_REQUESTS_PATH = "/api/v1/admin/withdraw/requests"


@contextmanager
def _authenticated(app, user_id: UUID, is_admin: bool = False):
    """Stub the auth middleware boundary so routes run as `user_id`."""
    fake_caller = MagicMock()
    fake_caller.id = user_id
    fake_caller.is_admin = is_admin
    fake_caller.status.value = "ACTIVE"
    with ExitStack() as stack:
        patch_user_repo = stack.enter_context(
            patch("vbwd.middleware.auth.UserRepository")
        )
        patch_auth_service = stack.enter_context(
            patch("vbwd.middleware.auth.AuthService")
        )
        patch_user_repo.return_value.find_by_id.return_value = fake_caller
        patch_auth_service.return_value.verify_token.return_value = str(user_id)
        yield


@contextmanager
def _payout_capable_plugin_manager(app, plugins):
    fake_manager = MagicMock()
    fake_manager.get_enabled_plugins.return_value = plugins
    with patch.object(app, "plugin_manager", fake_manager):
        yield


def _auth_headers():
    return {"Authorization": "Bearer test-token"}


class TestAuthRequired:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", PROVIDERS_PATH),
            ("POST", CREATE_PATH),
            ("GET", REQUESTS_PATH),
            ("GET", f"{REQUESTS_PATH}/{uuid4()}"),
            ("GET", ADMIN_REQUESTS_PATH),
            ("GET", f"{ADMIN_REQUESTS_PATH}/{uuid4()}"),
            ("POST", f"{ADMIN_REQUESTS_PATH}/{uuid4()}/approve"),
            ("POST", f"{ADMIN_REQUESTS_PATH}/{uuid4()}/reject"),
            ("POST", f"{ADMIN_REQUESTS_PATH}/{uuid4()}/pending"),
            ("DELETE", f"{ADMIN_REQUESTS_PATH}/{uuid4()}"),
        ],
    )
    def test_without_token_returns_401(self, client, method, path):
        response = client.open(path, method=method)
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", ADMIN_REQUESTS_PATH),
            ("GET", f"{ADMIN_REQUESTS_PATH}/{uuid4()}"),
            ("POST", f"{ADMIN_REQUESTS_PATH}/{uuid4()}/approve"),
            ("POST", f"{ADMIN_REQUESTS_PATH}/{uuid4()}/reject"),
            ("POST", f"{ADMIN_REQUESTS_PATH}/{uuid4()}/pending"),
            ("DELETE", f"{ADMIN_REQUESTS_PATH}/{uuid4()}"),
        ],
    )
    def test_admin_routes_refuse_non_admin_users(self, app, client, method, path):
        with _authenticated(app, uuid4(), is_admin=False):
            response = client.open(
                path, method=method, headers=_auth_headers(), json={}
            )
        assert response.status_code == 403


class TestProvidersEndpoint:
    def test_lists_only_enabled_payout_capable_implementers(self, app, client):
        payout_plugin = FakePayoutPlugin()
        plain_payment_plugin = MagicMock(spec=BasePlugin)
        with _authenticated(app, uuid4()), _payout_capable_plugin_manager(
            app, [payout_plugin, plain_payment_plugin]
        ):
            response = client.get(PROVIDERS_PATH, headers=_auth_headers())

        assert response.status_code == 200
        body = response.get_json()
        assert body["providers"] == [
            {
                "name": FAKE_PROVIDER_NAME,
                "destination_schema": FAKE_DESTINATION_SCHEMA,
            }
        ]

    def test_carries_the_conversion_config_block(self, app, client):
        with _authenticated(app, uuid4()), _payout_capable_plugin_manager(app, []):
            response = client.get(PROVIDERS_PATH, headers=_auth_headers())

        assert response.status_code == 200
        config_block = response.get_json()["config"]
        assert set(config_block.keys()) == {
            "currency",
            "token_to_currency_rate",
            "min_withdraw_tokens",
        }


class TestCreateValidation:
    def _post(self, client, payload):
        return client.post(CREATE_PATH, headers=_auth_headers(), json=payload)

    def test_below_minimum_returns_400(self, app, client):
        with _authenticated(app, uuid4()), _payout_capable_plugin_manager(
            app, [FakePayoutPlugin()]
        ):
            response = self._post(
                client,
                {
                    "provider": FAKE_PROVIDER_NAME,
                    "amount": 5,
                    "destination": {"email": "user@example.com"},
                },
            )
        assert response.status_code == 400
        assert "error" in response.get_json()

    def test_unknown_provider_returns_400(self, app, client):
        with _authenticated(app, uuid4()), _payout_capable_plugin_manager(app, []):
            response = self._post(
                client,
                {
                    "provider": "no-such-provider",
                    "amount": 150,
                    "destination": {"email": "user@example.com"},
                },
            )
        assert response.status_code == 400

    def test_unknown_balance_source_returns_400(self, app, client):
        with _authenticated(app, uuid4()), _payout_capable_plugin_manager(
            app, [FakePayoutPlugin()]
        ):
            response = self._post(
                client,
                {
                    "provider": FAKE_PROVIDER_NAME,
                    "amount": 150,
                    "destination": {"email": "user@example.com"},
                    "balance_source": "no-such-source",
                },
            )
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"provider": FAKE_PROVIDER_NAME},
            {"provider": FAKE_PROVIDER_NAME, "amount": "150"},
            {"provider": FAKE_PROVIDER_NAME, "amount": True},
            {"provider": FAKE_PROVIDER_NAME, "amount": -5},
            {"provider": FAKE_PROVIDER_NAME, "amount": 150, "destination": "x"},
        ],
    )
    def test_malformed_payloads_return_400(self, app, client, payload):
        with _authenticated(app, uuid4()), _payout_capable_plugin_manager(
            app, [FakePayoutPlugin()]
        ):
            response = self._post(client, payload)
        assert response.status_code == 400


class TestOwnerReads:
    def test_unknown_request_id_returns_404(self, app, client):
        with _authenticated(app, uuid4()):
            response = client.get(f"{REQUESTS_PATH}/{uuid4()}", headers=_auth_headers())
        assert response.status_code == 404

    def test_malformed_request_id_returns_404(self, app, client):
        with _authenticated(app, uuid4()):
            response = client.get(
                f"{REQUESTS_PATH}/not-a-uuid", headers=_auth_headers()
            )
        assert response.status_code == 404


@contextmanager
def _patched_admin_service(app, admin_id):
    """Run as an admin with the route's service factory patched to a mock,
    so route-level error mapping is exercised without a live DB/balance."""
    service = MagicMock()
    with _authenticated(app, admin_id, is_admin=True), patch(
        "plugins.withdraw.withdraw.routes._withdraw_service", return_value=service
    ):
        yield service


class TestAdminSetPendingRoute:
    def _path(self, request_id):
        return f"{ADMIN_REQUESTS_PATH}/{request_id}/pending"

    def test_success_returns_200_with_the_row(self, app, client):
        row = MagicMock()
        row.to_dict.return_value = {"id": "abc", "status": "pending"}
        with _patched_admin_service(app, uuid4()) as service:
            service.set_pending.return_value = row
            response = client.post(self._path(uuid4()), headers=_auth_headers())
        assert response.status_code == 200
        assert response.get_json() == {"request": {"id": "abc", "status": "pending"}}

    def test_malformed_id_returns_404(self, app, client):
        with _authenticated(app, uuid4(), is_admin=True):
            response = client.post(
                f"{ADMIN_REQUESTS_PATH}/not-a-uuid/pending", headers=_auth_headers()
            )
        assert response.status_code == 404

    def test_unknown_request_returns_404(self, app, client):
        with _patched_admin_service(app, uuid4()) as service:
            service.set_pending.side_effect = WithdrawRequestNotFoundError("nope")
            response = client.post(self._path(uuid4()), headers=_auth_headers())
        assert response.status_code == 404

    def test_wrong_status_returns_409(self, app, client):
        with _patched_admin_service(app, uuid4()) as service:
            service.set_pending.side_effect = InvalidWithdrawStatusError("bad")
            response = client.post(self._path(uuid4()), headers=_auth_headers())
        assert response.status_code == 409

    @pytest.mark.parametrize(
        "error", [InsufficientBalanceError("broke"), UnknownBalanceSourceError("x")]
    )
    def test_money_errors_return_400(self, app, client, error):
        with _patched_admin_service(app, uuid4()) as service:
            service.set_pending.side_effect = error
            response = client.post(self._path(uuid4()), headers=_auth_headers())
        assert response.status_code == 400


class TestAdminGetSingleRoute:
    def _path(self, request_id):
        return f"{ADMIN_REQUESTS_PATH}/{request_id}"

    def test_success_returns_200_with_the_row(self, app, client):
        row = MagicMock()
        row.to_dict.return_value = {"id": "abc", "status": "pending"}
        with _patched_admin_service(app, uuid4()) as service:
            service.admin_get.return_value = row
            response = client.get(self._path(uuid4()), headers=_auth_headers())
        assert response.status_code == 200
        assert response.get_json() == {"request": {"id": "abc", "status": "pending"}}

    def test_malformed_id_returns_404(self, app, client):
        with _authenticated(app, uuid4(), is_admin=True):
            response = client.get(
                f"{ADMIN_REQUESTS_PATH}/not-a-uuid", headers=_auth_headers()
            )
        assert response.status_code == 404
        assert response.get_json() == {"error": "Withdraw request not found"}

    def test_unknown_request_returns_404(self, app, client):
        with _patched_admin_service(app, uuid4()) as service:
            service.admin_get.side_effect = WithdrawRequestNotFoundError("nope")
            response = client.get(self._path(uuid4()), headers=_auth_headers())
        assert response.status_code == 404


class TestAdminApproveRoute:
    def _path(self, request_id):
        return f"{ADMIN_REQUESTS_PATH}/{request_id}/approve"

    def test_without_body_approves_with_no_override(self, app, client):
        row = MagicMock()
        row.to_dict.return_value = {"id": "abc", "status": "completed"}
        with _patched_admin_service(app, uuid4()) as service:
            service.approve.return_value = row
            response = client.post(self._path(uuid4()), headers=_auth_headers())
        assert response.status_code == 200
        assert response.get_json() == {"request": {"id": "abc", "status": "completed"}}
        service.approve.assert_called_once()
        _, kwargs = service.approve.call_args
        assert kwargs["payout_amount_override"] is None

    def test_valid_payout_amount_passes_the_override(self, app, client):
        row = MagicMock()
        row.to_dict.return_value = {"id": "abc", "status": "completed"}
        with _patched_admin_service(app, uuid4()) as service:
            service.approve.return_value = row
            response = client.post(
                self._path(uuid4()),
                headers=_auth_headers(),
                json={"payout_amount": "2.75"},
            )
        assert response.status_code == 200
        _, kwargs = service.approve.call_args
        assert kwargs["payout_amount_override"] == Decimal("2.75")

    @pytest.mark.parametrize("bad_amount", ["not-a-number", -5, 0, "-1.00", True])
    def test_invalid_payout_amount_returns_400(self, app, client, bad_amount):
        with _patched_admin_service(app, uuid4()) as service:
            response = client.post(
                self._path(uuid4()),
                headers=_auth_headers(),
                json={"payout_amount": bad_amount},
            )
        assert response.status_code == 400
        assert response.get_json() == {"error": "Invalid payout amount"}
        service.approve.assert_not_called()

    def test_malformed_id_returns_404(self, app, client):
        with _authenticated(app, uuid4(), is_admin=True):
            response = client.post(
                f"{ADMIN_REQUESTS_PATH}/not-a-uuid/approve", headers=_auth_headers()
            )
        assert response.status_code == 404

    def test_unknown_request_returns_404(self, app, client):
        with _patched_admin_service(app, uuid4()) as service:
            service.approve.side_effect = WithdrawRequestNotFoundError("nope")
            response = client.post(self._path(uuid4()), headers=_auth_headers())
        assert response.status_code == 404

    def test_non_pending_request_returns_409(self, app, client):
        with _patched_admin_service(app, uuid4()) as service:
            service.approve.side_effect = InvalidWithdrawStatusError("bad")
            response = client.post(self._path(uuid4()), headers=_auth_headers())
        assert response.status_code == 409


class TestAdminDeleteRoute:
    def _path(self, request_id):
        return f"{ADMIN_REQUESTS_PATH}/{request_id}"

    def test_success_returns_200(self, app, client):
        with _patched_admin_service(app, uuid4()) as service:
            service.delete.return_value = None
            response = client.delete(self._path(uuid4()), headers=_auth_headers())
        assert response.status_code == 200
        assert response.get_json() == {"success": True}

    def test_malformed_id_returns_404(self, app, client):
        with _authenticated(app, uuid4(), is_admin=True):
            response = client.delete(
                f"{ADMIN_REQUESTS_PATH}/not-a-uuid", headers=_auth_headers()
            )
        assert response.status_code == 404

    def test_unknown_request_returns_404(self, app, client):
        with _patched_admin_service(app, uuid4()) as service:
            service.delete.side_effect = WithdrawRequestNotFoundError("nope")
            response = client.delete(self._path(uuid4()), headers=_auth_headers())
        assert response.status_code == 404

    def test_wrong_status_returns_409(self, app, client):
        with _patched_admin_service(app, uuid4()) as service:
            service.delete.side_effect = InvalidWithdrawStatusError("bad")
            response = client.delete(self._path(uuid4()), headers=_auth_headers())
        assert response.status_code == 409
