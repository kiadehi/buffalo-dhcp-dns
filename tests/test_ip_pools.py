#!/usr/bin/env python3
"""
Unit tests for IP range filtering of VLAN pools
"""

import unittest
import ipaddress
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin")))
from sync_dhcp_dns import IPRangePool


class TestIPRangePools(unittest.TestCase):
    def setUp(self):
        self.vlan20 = IPRangePool("vlan20", "192.168.20.100", "192.168.20.200")
        self.vlan30 = IPRangePool("vlan30", "192.168.30.100", "192.168.30.200")
        self.vlan100 = IPRangePool("vlan100", "192.168.100.100", "192.168.100.200")

    def test_vlan20_bounds(self):
        # Under lower bound
        self.assertFalse(self.vlan20.contains(ipaddress.IPv4Address("192.168.20.99")))
        # Exact lower bound
        self.assertTrue(self.vlan20.contains(ipaddress.IPv4Address("192.168.20.100")))
        # Inside
        self.assertTrue(self.vlan20.contains(ipaddress.IPv4Address("192.168.20.150")))
        # Exact upper bound
        self.assertTrue(self.vlan20.contains(ipaddress.IPv4Address("192.168.20.200")))
        # Above upper bound
        self.assertFalse(self.vlan20.contains(ipaddress.IPv4Address("192.168.20.201")))
        # Gateway / Router IP
        self.assertFalse(self.vlan20.contains(ipaddress.IPv4Address("192.168.20.1")))

    def test_vlan30_bounds(self):
        self.assertFalse(self.vlan30.contains(ipaddress.IPv4Address("192.168.30.50")))
        self.assertTrue(self.vlan30.contains(ipaddress.IPv4Address("192.168.30.100")))
        self.assertTrue(self.vlan30.contains(ipaddress.IPv4Address("192.168.30.200")))
        self.assertFalse(self.vlan30.contains(ipaddress.IPv4Address("192.168.30.250")))

    def test_vlan100_bounds(self):
        self.assertFalse(self.vlan100.contains(ipaddress.IPv4Address("192.168.100.10")))
        self.assertTrue(self.vlan100.contains(ipaddress.IPv4Address("192.168.100.100")))
        self.assertTrue(self.vlan100.contains(ipaddress.IPv4Address("192.168.100.200")))
        self.assertFalse(self.vlan100.contains(ipaddress.IPv4Address("192.168.100.220")))

    def test_different_subnet_rejection(self):
        # Different VLANs entirely
        self.assertFalse(self.vlan20.contains(ipaddress.IPv4Address("192.168.1.100")))
        self.assertFalse(self.vlan30.contains(ipaddress.IPv4Address("10.0.0.100")))
        self.assertFalse(self.vlan100.contains(ipaddress.IPv4Address("172.16.20.100")))


if __name__ == "__main__":
    unittest.main()

