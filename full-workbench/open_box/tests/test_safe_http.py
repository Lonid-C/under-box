import socket
import unittest
from app.safe_http import UnsafeURL, validate_url


class SafeHTTPTests(unittest.TestCase):
    def resolver(self, host, port, type=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (self.address, port))]

    def test_private_and_metadata_addresses_are_rejected(self):
        for self.address in ("127.0.0.1", "10.0.0.4", "169.254.169.254", "::1"):
            with self.assertRaises(UnsafeURL):
                validate_url("https://example.test/", resolver=self.resolver)

    def test_public_fake_dns_is_allowed_without_network_request(self):
        self.address = "93.184.216.34"
        self.assertEqual(validate_url("https://example.test/", resolver=self.resolver).hostname, "example.test")
