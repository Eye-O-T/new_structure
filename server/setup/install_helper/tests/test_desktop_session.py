"""Desktop login survives closing the monitor until the server session expires."""

import unittest
from unittest.mock import Mock, patch

from server.desktop.admin.api import DesktopApi
from server.edge_onboarding.server_api import ServerApiClient, ServerApiError


class DesktopSessionTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.api = DesktopApi(
            "http://127.0.0.1", allow_insecure_http=True, session_store=self.store
        )

    def test_login_saves_refresh_token_and_logout_deletes_it(self):
        def login(_client, _username, _password):
            self.api._access_token = "access"
            self.api._refresh_token = "refresh"
            return {}

        with patch.object(ServerApiClient, "login", login), patch.object(
            self.api, "system_status", return_value={}
        ), patch.object(self.api, "_request", return_value={}):
            self.api.login("admin", "password")
            self.store.write.assert_called_once_with("admin", "refresh")
            self.api.logout()
        self.store.delete.assert_called_once_with()

    def test_saved_session_is_rotated_and_restored(self):
        self.store.read.return_value = ("admin", "old-refresh")
        response = {"access_token": "new-access", "refresh_token": "new-refresh"}
        with patch.object(ServerApiClient, "_request", return_value=response), patch.object(
            self.api, "system_status", return_value={}
        ):
            self.assertTrue(self.api.restore_session())
        self.assertEqual(self.api.session_username, "admin")
        self.assertEqual(self.api._access_token, "new-access")
        self.store.write.assert_called_once_with("admin", "new-refresh")

    def test_expired_saved_session_is_removed(self):
        self.store.read.return_value = ("admin", "expired-refresh")
        error = ServerApiError(401, "AUTH_REQUIRED", "Refresh token expired")
        with patch.object(ServerApiClient, "_request", side_effect=error):
            self.assertFalse(self.api.restore_session())
        self.store.delete.assert_called_once_with()
        self.assertIsNone(self.api._access_token)


if __name__ == "__main__":
    unittest.main()
