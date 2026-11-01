#!/usr/bin/env python3
"""
Unit tests for DNS synchronization logic and state diff tracking
"""

import unittest
import tempfile
import sys
import os
import shutil

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin")))
from sync_dhcp_dns import BuffaloDHCPSyncApp


class MockDNSManager:
    def __init__(self):
        self.added = []
        self.deleted = []
        self.updated = []

    def add_a_record(self, hostname: str, ip_str: str) -> bool:
        self.added.append((hostname, ip_str))
        return True

    def delete_a_record(self, hostname: str, ip_str: str) -> bool:
        self.deleted.append((hostname, ip_str))
        return True

    def update_a_record(self, hostname: str, old_ip: str, new_ip: str) -> bool:
        self.updated.append((hostname, old_ip, new_ip))
        return True


class TestDNSSyncLogic(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmp_dir, "state.json")
        self.config_file = os.path.join(self.tmp_dir, "config.ini")

        with open(self.config_file, "w") as f:
            f.write(f"""
[switch]
host = 127.0.0.1
[samba]
server = 127.0.0.1
domain_zone = corp.example.local
[vlan_pools]
vlan20 = 192.168.20.100 - 192.168.20.200
vlan30 = 192.168.30.100 - 192.168.30.200
vlan100 = 192.168.100.100 - 192.168.100.200
[hostname]
template = host-{{ip_dashed}}
[storage]
state_file = {self.state_file}
""")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_full_lifecycle(self):
        # --- Cycle 1: Add initial leases ---
        input1 = os.path.join(self.tmp_dir, "input1.txt")
        with open(input1, "w") as f:
            f.write("""
 192.168.20.102       00:11:22:33:44:01        Oct 31 08:54:24  2026
 192.168.20.103       00:11:22:33:44:02        Oct 31 08:31:42  2026
 192.168.100.100      00:11:22:33:44:03        Oct 31 08:50:11  2026
 192.168.20.1         00:11:22:33:44:55        Oct 31 08:00:00  2026 # Gateway out of pool!
 192.168.50.10        66:77:88:99:00:11        Oct 31 08:00:00  2026 # Out of VLAN!
""")

        app = BuffaloDHCPSyncApp(self.config_file, input_file=input1)
        mock_dns = MockDNSManager()
        app.dns_manager = mock_dns

        added, updated, deleted = app.sync()
        self.assertEqual(added, 3)
        self.assertEqual(updated, 0)
        self.assertEqual(deleted, 0)
        self.assertEqual(len(mock_dns.added), 3)
        # Out-of-pool IPs were not added
        self.assertNotIn(("host-192-168-20-1", "192.168.20.1"), mock_dns.added)
        self.assertNotIn(("host-192-168-50-10", "192.168.50.10"), mock_dns.added)

        # Verify state file contains 3 records
        self.assertEqual(len(app.state_manager.state), 3)
        self.assertIn("192.168.20.102", app.state_manager.state)

        # --- Cycle 2: Same leases (no changes) ---
        app2 = BuffaloDHCPSyncApp(self.config_file, input_file=input1)
        mock_dns2 = MockDNSManager()
        app2.dns_manager = mock_dns2

        added2, updated2, deleted2 = app2.sync()
        self.assertEqual(added2, 0)
        self.assertEqual(updated2, 0)
        self.assertEqual(deleted2, 0)
        self.assertEqual(len(mock_dns2.added), 0)
        self.assertEqual(len(mock_dns2.deleted), 0)

        # --- Cycle 3: Lease 192.168.20.102 expired/released ---
        input2 = os.path.join(self.tmp_dir, "input2.txt")
        with open(input2, "w") as f:
            f.write("""
 192.168.20.103       00:11:22:33:44:02        Oct 31 08:31:42  2026
 192.168.100.100      00:11:22:33:44:03        Oct 31 08:50:11  2026
""")
        app3 = BuffaloDHCPSyncApp(self.config_file, input_file=input2)
        mock_dns3 = MockDNSManager()
        app3.dns_manager = mock_dns3

        added3, updated3, deleted3 = app3.sync()
        self.assertEqual(added3, 0)
        self.assertEqual(updated3, 0)
        self.assertEqual(deleted3, 1)
        self.assertEqual(len(mock_dns3.deleted), 1)
        self.assertEqual(mock_dns3.deleted[0], ("host-192-168-20-102", "192.168.20.102"))

        # Verify state file now contains only 2 records
        self.assertEqual(len(app3.state_manager.state), 2)
        self.assertNotIn("192.168.20.102", app3.state_manager.state)


if __name__ == "__main__":
    unittest.main()
