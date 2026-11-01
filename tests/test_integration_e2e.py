#!/usr/bin/env python3
"""
End-to-end integration test:
  Mock BS-G3024MR Telnet Server <--> buffalo_fetch_dhcp.exp <--> sync_dhcp_dns.py
"""

import unittest
import tempfile
import sys
import os
import shutil
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from mock_buffalo_switch import MockBSG3024MRServer
from sync_dhcp_dns import BuffaloDHCPSyncApp


class MockDNSManager:
    def __init__(self):
        self.added = []
        self.deleted = []

    def add_a_record(self, hostname: str, ip_str: str) -> bool:
        self.added.append((hostname, ip_str))
        return True

    def delete_a_record(self, hostname: str, ip_str: str) -> bool:
        self.deleted.append((hostname, ip_str))
        return True


class TestEndToEndIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MockBSG3024MRServer(host="127.0.0.1", port=2328)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmp_dir, "state.json")
        self.hosts_map = os.path.join(self.tmp_dir, "hosts.map")
        self.config_file = os.path.join(self.tmp_dir, "config.ini")
        self.expect_script = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "bin", "buffalo_fetch_dhcp.exp")
        )

        with open(self.hosts_map, "w") as f:
            f.write("""
00:11:22:33:44:01   reception-pc
192.168.100.100     entrance-kiosk
""")

        with open(self.config_file, "w") as f:
            f.write(f"""
[switch]
host = 127.0.0.1
port = 2328
user = admin
password = adminpass
prompt = BS-G3024MR
timeout = 5
expect_script = {self.expect_script}

[samba]
server = 127.0.0.1
domain_zone = internal.corp
auth_method = kerberos

[vlan_pools]
vlan20 = 192.168.20.100 - 192.168.20.200
vlan30 = 192.168.30.100 - 192.168.30.200
vlan100 = 192.168.100.100 - 192.168.100.200

[hostname]
hosts_map_file = {self.hosts_map}
template = dhcp-{{mac_clean}}

[storage]
state_file = {self.state_file}
""")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_live_telnet_and_sync(self):
        app = BuffaloDHCPSyncApp(self.config_file)
        mock_dns = MockDNSManager()
        app.dns_manager = mock_dns

        added, updated, deleted = app.sync()

        # All 3 leases from mock switch:
        # 192.168.20.102 (00:11:22:33:44:01) -> reception-pc (from hosts.map)
        # 192.168.20.103 (00:11:22:33:44:02) -> dhcp-001122334402 (from template)
        # 192.168.100.100 (00:11:22:33:44:03) -> entrance-kiosk (from hosts.map IP)
        self.assertEqual(added, 3)
        self.assertEqual(updated, 0)
        self.assertEqual(deleted, 0)

        added_map = dict(mock_dns.added)
        self.assertEqual(added_map.get("reception-pc"), "192.168.20.102")
        self.assertEqual(added_map.get("dhcp-001122334402"), "192.168.20.103")
        self.assertEqual(added_map.get("entrance-kiosk"), "192.168.100.100")

        # Verify state file
        self.assertIn("192.168.20.102", app.state_manager.state)
        self.assertEqual(app.state_manager.state["192.168.20.102"]["hostname"], "reception-pc")


if __name__ == "__main__":
    unittest.main()
