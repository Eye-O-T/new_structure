import unittest

import httpx

from server.services.external.app.clients.data import DataClient


class DataClientCameraListTests(unittest.IsolatedAsyncioTestCase):
    async def test_unscoped_list_omits_user_id_query_parameter(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={"items": [], "limit": 200, "offset": 0},
            )

        client = DataClient(
            base_url="http://data",
            health_url="http://data/health/ready",
            internal_token="test-token",
            transport=httpx.MockTransport(handle),
        )
        try:
            await client.list_cameras(user_id=None, limit=200, offset=0)
        finally:
            await client.close()

        self.assertEqual(len(requests), 1)
        self.assertEqual(
            dict(requests[0].url.params), {"limit": "200", "offset": "0"}
        )


if __name__ == "__main__":
    unittest.main()
