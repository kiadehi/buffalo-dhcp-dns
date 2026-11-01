#!/usr/bin/env python3
"""
Unit tests for HostnameResolver (hosts.map and template generation)
"""

import unittest
import tempfile
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin")))
from sync_dhcp_dns import HostnameResolver, DHCPLease


class TestHostnameResolver(unittest.TestCase):
    def setUp(self):
        self.tmp_map = tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8")
        self.tmp_map.write("""
# Static map test
00:11:22:33:44:01    reception-pc01     # Front reception
00-11-22-33-44-02    meeting-display
192.168.100.100      guest-kiosk
""")
        self.tmp_map.close()

    def tearDown(self):
        if os.path.exists(self.tmp_map.name):
            os.remove(self.tmp_map.name)

    def test_static_map_resolution(self):
        resolver = HostnameResolver(map_file_path=self.tmp_map.name)

        lease1 = DHCPLease("192.168.20.102", "00:11:22:33:44:01", "Oct 31 08:54:24 2026")
        self.assertEqual(resolver.resolve(lease1), "reception-pc01")

        lease2 = DHCPLease("192.168.20.103", "00:11:22:33:44:02", "Oct 31 08:31:42 2026")
        self.assertEqual(resolver.resolve(lease2), "meeting-display")

        lease3 = DHCPLease("192.168.100.100", "00:11:22:33:44:55", "Oct 31 08:50:11 2026")
        self.assertEqual(resolver.resolve(lease3), "guest-kiosk")

    def test_fallback_templates(self):
        lease = DHCPLease("192.168.20.150", "aa:bb:cc:dd:ee:ff", "Oct 31 08:00:00 2026")

        r1 = HostnameResolver(template="dhcp-{mac_clean}")
        self.assertEqual(r1.resolve(lease), "dhcp-aabbccddeeff")

        r2 = HostnameResolver(template="dhcp-vlan{vlan}-{last_octet}")
        self.assertEqual(r2.resolve(lease), "dhcp-vlan20-150")

        r3 = HostnameResolver(template="host-{ip_dashed}")
        self.assertEqual(r3.resolve(lease), "host-192-168-20-150")


if __name__ == "__main__":
    unittest.main()

