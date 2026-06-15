"""Route-level specs for the withdraw API (S79) — fixed contract.

The fe-user withdraw plugin is built against exactly these shapes in
parallel. Auth is stubbed at the middleware boundary (house pattern);
the providers endpoint discovers payout-capable plugins via a patched
`plugin_manager` so no real provider plugin is needed (they are
Slice 2).
"""
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from vbwd.plugins.base import BasePlugin

from plugins.withdraw.tests.unit.fakes import (
    FAKE_DESTINATION_SCHEMA,
    FAKE_PROVIDER_NAME,
    FakePayoutPlugin,
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
            ("POST", f"{ADMIN_REQUESTS_PATH}/{uuid4()}/approve"),
            ("POST", f"{ADMIN_REQUESTS_PATH}/{uuid4()}/reject"),
        ],
    )
    def test_without_token_returns_401(self, client, method, path):
        response = client.open(path, method=method)
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", ADMIN_REQUESTS_PATH),
            ("POST", f"{ADMIN_REQUESTS_PATH}/{uuid4()}/approve"),
            ("POST", f"{ADMIN_REQUESTS_PATH}/{uuid4()}/reject"),
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
