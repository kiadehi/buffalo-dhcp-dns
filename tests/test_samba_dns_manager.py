#!/usr/bin/env python3
"""
Unit tests for SambaDNSManager authentication and server resolution
"""

import os
import socket
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin")))
from sync_dhcp_dns import SambaDNSManager


class TestSambaDNSManager(unittest.TestCase):
    def test_ip_address_auto_resolution(self):
        """127.0.0.1 and ::1 should be automatically converted to hostname."""
        local_host = socket.gethostname()
        mgr1 = SambaDNSManager(server="127.0.0.1", domain_zone="corp.example.com")
        self.assertEqual(mgr1.server, local_host)

        mgr2 = SambaDNSManager(server="::1", domain_zone="corp.example.com")
        self.assertEqual(mgr2.server, local_host)

        # Explicit hostname should be preserved
        mgr3 = SambaDNSManager(server="dc1.corp.example.com", domain_zone="corp.example.com")
        self.assertEqual(mgr3.server, "dc1.corp.example.com")

    def test_machine_account_auth_args(self):
        """machine_account auth should return ['-P']"""
        mgr = SambaDNSManager(
            server="localhost",
            domain_zone="corp.example.com",
            auth_method="machine_account",
        )
        self.assertEqual(mgr._get_auth_args(), ["-P"])

        mgr_alias = SambaDNSManager(
            server="localhost",
            domain_zone="corp.example.com",
            auth_method="local",
        )
        self.assertEqual(mgr_alias._get_auth_args(), ["-P"])

    def test_credentials_auth_args(self):
        """credentials auth should return ['-U', 'user%pass']"""
        mgr = SambaDNSManager(
            server="localhost",
            domain_zone="corp.example.com",
            auth_method="credentials",
            samba_user="admin",
            samba_password="secretpassword",
        )
        self.assertEqual(mgr._get_auth_args(), ["-U", "admin%secretpassword"])

    @patch("subprocess.run")
    def test_kerberos_flag_detection(self, mock_run):
        """Should detect --use-kerberos=required when supported by samba-tool."""
        mgr = SambaDNSManager(
            server="localhost",
            domain_zone="corp.example.com",
            auth_method="kerberos",
        )

        # Case 1: samba-tool supports --use-kerberos
        mock_run.return_value = MagicMock(returncode=0, stdout="--use-kerberos=required", stderr="")
        flag = mgr._detect_kerberos_flag()
        self.assertEqual(flag, ["--use-kerberos=required"])

        # Case 2: cached flag is reused
        self.assertEqual(mgr._detect_kerberos_flag(), ["--use-kerberos=required"])

    @patch("subprocess.run")
    def test_kerberos_flag_legacy_fallback(self, mock_run):
        """Should fall back to -k yes when --use-kerberos is not supported."""
        mgr = SambaDNSManager(
            server="localhost",
            domain_zone="corp.example.com",
            auth_method="kerberos",
        )

        mock_run.return_value = MagicMock(returncode=0, stdout="legacy help without kerberos flag", stderr="")
        flag = mgr._detect_kerberos_flag()
        self.assertEqual(flag, ["-k", "yes"])


if __name__ == "__main__":
    unittest.main()
