import unittest

from server.setup.install_helper.server_api import ServerApiClient


class ServerApiPolicyTests(unittest.TestCase):
    def test_http_requires_explicit_allowance(self):
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            ServerApiClient("http://192.168.50.1")
        ServerApiClient("http://192.168.50.1", allow_insecure_http=True)

    def test_https_and_loopback_http_remain_allowed(self):
        ServerApiClient("https://192.168.50.1")
        ServerApiClient("http://127.0.0.1")


if __name__ == "__main__":
    unittest.main()
