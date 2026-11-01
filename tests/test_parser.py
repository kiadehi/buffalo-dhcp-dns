#!/usr/bin/env python3
"""
Unit tests for BUFFALO BS-G3024MR DHCP table parser
"""

import unittest
import sys
import os

# Add bin to import path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin")))
from sync_dhcp_dns import DHCPLease, BuffaloDHCPSyncApp, DHCP_LINE_PATTERN


class TestDHCPParser(unittest.TestCase):
    SAMPLE_OUTPUT = """
BUFFALO BS-G3024MR# show ip dhcp binding

 IP                   MAC                      LeaseTime
 -----------------------------------------------------------------
 192.168.20.102       00:11:22:33:44:01        Oct 31 08:54:24  2026
 192.168.20.103       00:11:22:33:44:02        Oct 31 08:31:42  2026
 192.168.100.100      00:11:22:33:44:03        Oct 31 08:50:11  2026

BS-G3024MR# 
"""

    def test_regex_matching(self):
        matches = list(DHCP_LINE_PATTERN.finditer(self.SAMPLE_OUTPUT))
        self.assertEqual(len(matches), 3)

        d0 = matches[0].groupdict()
        self.assertEqual(d0["ip"], "192.168.20.102")
        self.assertEqual(d0["mac"], "00:11:22:33:44:01")
        self.assertEqual(d0["leasetime"], "Oct 31 08:54:24  2026")

        d1 = matches[1].groupdict()
        self.assertEqual(d1["ip"], "192.168.20.103")
        self.assertEqual(d1["mac"], "00:11:22:33:44:02")

        d2 = matches[2].groupdict()
        self.assertEqual(d2["ip"], "192.168.100.100")
        self.assertEqual(d2["mac"], "00:11:22:33:44:03")

    def test_mac_normalization(self):
        self.assertEqual(DHCPLease.normalize_mac("00:11:22:33:44:01"), "00:11:22:33:44:01")
        self.assertEqual(DHCPLease.normalize_mac("00:11:22:33:44:01".upper()), "00:11:22:33:44:01")
        self.assertEqual(DHCPLease.normalize_mac("00-11-22-33-44-01"), "00:11:22:33:44:01")
        self.assertEqual(DHCPLease.normalize_mac("0011.2233.4401"), "00:11:22:33:44:01")
        self.assertEqual(DHCPLease.normalize_mac("001122334401"), "00:11:22:33:44:01")

    def test_datetime_parsing(self):
        lease = DHCPLease("192.168.20.102", "00:11:22:33:44:01", "Oct 31 08:54:24  2026")
        self.assertIsNotNone(lease.lease_time)
        self.assertEqual(lease.lease_time.year, 2026)
        self.assertEqual(lease.lease_time.month, 10)
        self.assertEqual(lease.lease_time.day, 31)
        self.assertEqual(lease.lease_time.hour, 8)
        self.assertEqual(lease.lease_time.minute, 54)
        self.assertEqual(lease.lease_time.second, 24)


if __name__ == "__main__":
    unittest.main()

