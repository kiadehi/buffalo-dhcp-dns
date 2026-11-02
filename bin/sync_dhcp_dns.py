#!/usr/bin/env python3
"""
BUFFALO BS-G3024MR DHCP Lease to Samba AD DNS Sync Daemon / Script

Description:
    Connects to BUFFALO BS-G3024MR L3 switch via Telnet (using expect automation),
    extracts active DHCP lease bindings for specified VLAN pools, resolves or
    generates hostnames, and synchronizes A (and optionally PTR) records in
    Samba 4 Active Directory Internal DNS using samba-tool dns.

Author: Antigravity Pair Programming
"""

import argparse
import configparser
import datetime
import fcntl
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
from typing import Dict, List, Optional, Set, Tuple


# Default regex for parsing BS-G3024MR "show ip dhcp binding"
# Example line:
# 192.168.20.102       00:11:22:33:44:01        Oct 31 08:54:24  2026
DHCP_LINE_PATTERN = re.compile(
    r"^\s*(?P<ip>\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+"
    r"(?P<mac>[0-9a-fA-F:.-]{11,17})\s+"
    r"(?P<leasetime>[^#\r\n]+?)\s*(?:#.*)?$",
    re.MULTILINE,
)

logger = logging.getLogger("buffalo_dhcp_dns")


class DHCPLease:
    """Represents a single DHCP lease entry from the switch."""

    def __init__(self, ip: str, mac: str, lease_time_str: str):
        self.ip_str = ip
        self.ip = ipaddress.IPv4Address(ip)
        self.mac = self.normalize_mac(mac)
        self.lease_time_str = lease_time_str
        self.lease_time: Optional[datetime.datetime] = self.parse_lease_time(lease_time_str)

    @staticmethod
    def normalize_mac(raw_mac: str) -> str:
        """Normalizes MAC to lowercase colon-separated format (e.g. aa:bb:cc:dd:ee:ff)."""
        clean = re.sub(r"[^0-9a-fA-F]", "", raw_mac).lower()
        if len(clean) != 12:
            return raw_mac.lower()
        return ":".join(clean[i : i + 2] for i in range(0, 12, 2))

    @staticmethod
    def parse_lease_time(time_str: str) -> Optional[datetime.datetime]:
        """Parses switch timestamp like 'Oct 31 08:54:24 2026'."""
        clean_time = re.sub(r"\s+", " ", time_str.strip())
        try:
            return datetime.datetime.strptime(clean_time, "%b %d %H:%M:%S %Y")
        except ValueError:
            logger.warning("Could not parse lease timestamp: %r", time_str)
            return None

    def __repr__(self) -> str:
        return f"<DHCPLease {self.ip_str} ({self.mac}) exp={self.lease_time_str}>"


class IPRangePool:
    """Represents an allowed IPv4 range (e.g. 192.168.20.100 - 192.168.20.200)."""

    def __init__(self, name: str, start_ip: str, end_ip: str):
        self.name = name
        self.start = ipaddress.IPv4Address(start_ip)
        self.end = ipaddress.IPv4Address(end_ip)
        if self.start > self.end:
            raise ValueError(f"Start IP {start_ip} cannot be greater than End IP {end_ip}")

    def contains(self, ip: ipaddress.IPv4Address) -> bool:
        return self.start <= ip <= self.end

    def __repr__(self) -> str:
        return f"Pool({self.name}: {self.start} - {self.end})"


class HostnameResolver:
    """Resolves hostnames via static hosts.map, NetBIOS lookup, or naming template."""

    def __init__(
        self,
        map_file_path: Optional[str] = None,
        enable_netbios: bool = False,
        template: str = "dhcp-{mac_clean}",
    ):
        self.map_file_path = map_file_path
        self.enable_netbios = enable_netbios
        self.template = template
        self.mac_map: Dict[str, str] = {}
        self.ip_map: Dict[str, str] = {}
        self.load_map_file()

    def load_map_file(self) -> None:
        if not self.map_file_path or not os.path.isfile(self.map_file_path):
            return

        logger.info("Loading static hostname map from %s", self.map_file_path)
        with open(self.map_file_path, "r", encoding="utf-8") as f:
            for line_no, raw_line in enumerate(f, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                parts = line.split()
                if len(parts) < 2:
                    logger.warning("Line %d in %s is malformed: %r", line_no, self.map_file_path, line)
                    continue

                key = parts[0].strip()
                hostname = parts[1].strip()

                # Validate hostname format (DNS compliant)
                if not self.is_valid_dns_name(hostname):
                    logger.warning(
                        "Line %d has invalid DNS hostname %r. Skipping.", line_no, hostname
                    )
                    continue

                if ":" in key or "-" in key or (len(key) == 12 and all(c in "0123456789abcdefABCDEF" for c in key)):
                    norm_mac = DHCPLease.normalize_mac(key)
                    self.mac_map[norm_mac] = hostname
                else:
                    try:
                        ipaddress.IPv4Address(key)
                        self.ip_map[key] = hostname
                    except ValueError:
                        logger.warning("Unrecognized key %r on line %d in %s", key, line_no, self.map_file_path)

        logger.info("Loaded %d MAC mappings and %d IP mappings.", len(self.mac_map), len(self.ip_map))

    @staticmethod
    def is_valid_dns_name(name: str) -> bool:
        if not name or len(name) > 63:
            return False
        return bool(re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$", name))

    def query_netbios(self, ip_str: str) -> Optional[str]:
        """Optionally query client NetBIOS computer name using nmblookup."""
        if not self.enable_netbios or not shutil.which("nmblookup"):
            return None

        try:
            cmd = ["nmblookup", "-A", ip_str]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if res.returncode == 0:
                # Look for <00> - B <ACTIVE> <UNIQUE> line
                for line in res.stdout.splitlines():
                    match = re.search(r"^\s*([A-Za-z0-9_-]+)\s+<00>\s+-\s+<ACTIVE>", line, re.IGNORECASE)
                    if match:
                        name = match.group(1).strip().lower()
                        if self.is_valid_dns_name(name):
                            logger.debug("Resolved NetBIOS name %s for %s", name, ip_str)
                            return name
        except Exception as e:
            logger.debug("NetBIOS lookup for %s failed: %s", ip_str, e)
        return None

    def resolve(self, lease: DHCPLease) -> str:
        # 1. Static MAC map
        if lease.mac in self.mac_map:
            return self.mac_map[lease.mac]

        # 2. Static IP map
        if lease.ip_str in self.ip_map:
            return self.ip_map[lease.ip_str]

        # 3. Dynamic NetBIOS resolution
        netbios_name = self.query_netbios(lease.ip_str)
        if netbios_name:
            return netbios_name

        # 4. Fallback template generation
        mac_clean = lease.mac.replace(":", "")
        mac_dashed = lease.mac.replace(":", "-")
        ip_dashed = lease.ip_str.replace(".", "-")
        octets = lease.ip_str.split(".")
        last_octet = octets[-1]
        vlan = octets[-2]

        generated = self.template.format(
            mac_clean=mac_clean,
            mac_dashed=mac_dashed,
            mac=mac_clean,
            ip_dashed=ip_dashed,
            ip=lease.ip_str,
            vlan=vlan,
            last_octet=last_octet,
        )

        # Sanitize to valid DNS label
        clean_name = re.sub(r"[^a-zA-Z0-9\-]", "-", generated).lower()
        clean_name = re.sub(r"^-+|-+$", "", clean_name)
        return clean_name or f"dhcp-{mac_clean}"


class SambaDNSManager:
    """Interacts with Samba Internal DNS via samba-tool dns CLI."""

    def __init__(
        self,
        server: str,
        domain_zone: str,
        auth_method: str = "kerberos",
        samba_user: Optional[str] = None,
        samba_password: Optional[str] = None,
        keytab_path: Optional[str] = None,
        krb5_principal: Optional[str] = None,
        sync_ptr: bool = False,
        reverse_zones: Optional[Dict[str, str]] = None,
        dry_run: bool = False,
    ):
        self.server = server
        self.domain_zone = domain_zone
        self.auth_method = auth_method
        self.samba_user = samba_user
        self.samba_password = samba_password
        self.keytab_path = keytab_path
        self.krb5_principal = krb5_principal
        self.sync_ptr = sync_ptr
        self.reverse_zones = reverse_zones or {}
        self.dry_run = dry_run
        self.samba_tool_bin = shutil.which("samba-tool") or "/usr/bin/samba-tool"

        # If server is numeric 127.0.0.1 or ::1, resolve to local hostname or DC FQDN
        # because Kerberos SPN and RPC binding reject numeric IP addresses with NT_STATUS_INVALID_PARAMETER.
        if self.server in ("127.0.0.1", "::1"):
            local_host = socket.gethostname()
            logger.warning(
                "Samba server is set to '%s'. Kerberos/RPC authentication fails on numeric IP addresses (NT_STATUS_INVALID_PARAMETER). "
                "Automatically using local hostname '%s'. (Ensure 'server = %s' or DC FQDN is configured in config.ini)",
                self.server,
                local_host,
                local_host,
            )
            self.server = local_host

    def _detect_kerberos_flag(self) -> List[str]:
        """Detects whether samba-tool supports modern --use-kerberos=required or legacy -k yes."""
        if hasattr(self, "_cached_kerberos_flag"):
            return self._cached_kerberos_flag

        flag = ["-k", "yes"]
        try:
            res = subprocess.run(
                [self.samba_tool_bin, "dns", "add", "--help"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if "--use-kerberos" in res.stdout or "--use-kerberos" in res.stderr:
                flag = ["--use-kerberos=required"]
        except Exception:
            pass

        self._cached_kerberos_flag = flag
        return flag

    def _ensure_kerberos(self) -> None:
        """If keytab is provided, ensures valid Kerberos ticket via kinit."""
        if self.auth_method.lower().strip() != "kerberos":
            return

        if self.keytab_path and os.path.isfile(self.keytab_path):
            principal = (
                self.krb5_principal
                or f"{self.samba_user or 'Administrator'}@{self.domain_zone.upper()}"
            )
            logger.debug(
                "Refreshing Kerberos ticket with keytab %s for %s", self.keytab_path, principal
            )
            try:
                subprocess.run(
                    ["kinit", "-k", "-t", self.keytab_path, principal],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except Exception as e:
                logger.error("Failed to run kinit: %s", e)
        else:
            try:
                res = subprocess.run(["klist", "-s"], capture_output=True, timeout=5)
                if res.returncode != 0:
                    logger.warning(
                        "No active Kerberos ticket found in cache (klist -s returned %d). "
                        "Please run 'kinit' or configure 'keytab_path' in config.ini, "
                        "or set 'auth_method = machine_account' (recommended for Samba DC) / 'auth_method = credentials'.",
                        res.returncode,
                    )
            except Exception:
                pass

    def _get_auth_args(self) -> List[str]:
        method = self.auth_method.lower().strip()
        if method == "kerberos":
            self._ensure_kerberos()
            return self._detect_kerberos_flag()
        elif method in ("machine_account", "machine", "local", "-p"):
            return ["-P"]
        elif method == "credentials":
            if not self.samba_user:
                raise ValueError("samba_user is required for credentials auth")
            cred = f"{self.samba_user}%{self.samba_password or ''}"
            return ["-U", cred]
        return []

    def run_samba_tool(self, subargs: List[str]) -> Tuple[int, str, str]:
        """Runs a samba-tool dns command."""
        cmd = [self.samba_tool_bin, "dns"] + subargs + self._get_auth_args()
        # Redact password from log output
        redacted_cmd = [re.sub(r"%.*$", "%***", arg) if "-U" in cmd and "%" in arg else arg for arg in cmd]
        logger.debug("Executing: %s", " ".join(redacted_cmd))

        if self.dry_run and subargs[0] in ("add", "delete", "update"):
            logger.info("[DRY-RUN] Would execute: %s", " ".join(redacted_cmd))
            return 0, "", ""

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return res.returncode, res.stdout, res.stderr
        except Exception as e:
            logger.error("samba-tool execution failed: %s", e)
            return 1, "", str(e)

    def query_record(self, zone: str, name: str, record_type: str = "A") -> Optional[List[str]]:
        """Queries Samba DNS for existing records."""
        ret, stdout, stderr = self.run_samba_tool(["query", self.server, zone, name, record_type])
        if ret != 0:
            return None

        # Parse query output
        values = []
        for line in stdout.splitlines():
            line_str = line.strip()
            if line_str.startswith(f"{record_type}:"):
                # e.g. "A: 192.168.20.102 (flags=f0, serial=1, ttl=900)"
                match = re.search(rf"^{record_type}:\s+([^\s]+)", line_str)
                if match:
                    values.append(match.group(1))
        return values

    def add_a_record(self, hostname: str, ip_str: str) -> bool:
        """Adds an A record to the domain zone."""
        logger.info("Adding A record: %s.%s -> %s", hostname, self.domain_zone, ip_str)
        ret, out, err = self.run_samba_tool(["add", self.server, self.domain_zone, hostname, "A", ip_str])
        if ret != 0:
            combined_err = f"{out} {err}".lower()
            if "already_exists" in combined_err or "already exists" in combined_err:
                logger.info("A record %s already exists in DNS. Adopting into state.", hostname)
            else:
                logger.error("Failed to add A record %s: %s %s", hostname, out.strip(), err.strip())
                return False

        if self.sync_ptr:
            self.add_ptr_record(hostname, ip_str)
        return True

    def delete_a_record(self, hostname: str, ip_str: str) -> bool:
        """Deletes an A record from the domain zone."""
        logger.info("Deleting A record: %s.%s -> %s", hostname, self.domain_zone, ip_str)
        ret, out, err = self.run_samba_tool(["delete", self.server, self.domain_zone, hostname, "A", ip_str])
        if ret != 0:
            # If record didn't exist or already removed, log as warning
            logger.warning("Delete A record returned code %d: %s %s", ret, out.strip(), err.strip())
            # We don't fail hard on delete if already gone
        if self.sync_ptr:
            self.delete_ptr_record(hostname, ip_str)
        return True

    def update_a_record(self, hostname: str, old_ip: str, new_ip: str) -> bool:
        """Updates an existing A record from old_ip to new_ip."""
        logger.info("Updating A record: %s.%s: %s -> %s", hostname, self.domain_zone, old_ip, new_ip)
        ret, out, err = self.run_samba_tool(["update", self.server, self.domain_zone, hostname, "A", old_ip, new_ip])
        if ret != 0:
            logger.warning("Update failed, falling back to delete + add: %s %s", out.strip(), err.strip())
            self.delete_a_record(hostname, old_ip)
            return self.add_a_record(hostname, new_ip)

        if self.sync_ptr:
            self.delete_ptr_record(hostname, old_ip)
            self.add_ptr_record(hostname, new_ip)
        return True

    def _get_ptr_info(self, ip_str: str) -> Tuple[Optional[str], Optional[str]]:
        """Calculates reverse zone and PTR record name from IPv4 address."""
        octets = ip_str.split(".")
        if len(octets) != 4:
            return None, None

        # Check explicit reverse zone map (e.g. "192.168.20" -> "20.168.192.in-addr.arpa")
        subnet_24 = f"{octets[0]}.{octets[1]}.{octets[2]}"
        reverse_zone = self.reverse_zones.get(subnet_24) or f"{octets[2]}.{octets[1]}.{octets[0]}.in-addr.arpa"
        ptr_name = octets[3]
        return reverse_zone, ptr_name

    def add_ptr_record(self, hostname: str, ip_str: str) -> bool:
        zone, ptr_name = self._get_ptr_info(ip_str)
        if not zone or not ptr_name:
            return False

        fqdn = f"{hostname}.{self.domain_zone}."
        logger.info("Adding PTR record: %s in %s -> %s", ptr_name, zone, fqdn)
        ret, out, err = self.run_samba_tool(["add", self.server, zone, ptr_name, "PTR", fqdn])
        if ret != 0:
            combined_err = f"{out} {err}".lower()
            if "already_exists" in combined_err or "already exists" in combined_err:
                logger.debug("PTR record %s already exists in %s.", ptr_name, zone)
            else:
                logger.warning("Failed to add PTR record %s in %s: %s", ptr_name, zone, err.strip() or out.strip())
                return False
        return True

    def delete_ptr_record(self, hostname: str, ip_str: str) -> bool:
        zone, ptr_name = self._get_ptr_info(ip_str)
        if not zone or not ptr_name:
            return False

        fqdn = f"{hostname}.{self.domain_zone}."
        logger.info("Deleting PTR record: %s in %s -> %s", ptr_name, zone, fqdn)
        ret, out, err = self.run_samba_tool(["delete", self.server, zone, ptr_name, "PTR", fqdn])
        if ret != 0:
            logger.warning("Failed to delete PTR record %s in %s: %s", ptr_name, zone, err.strip() or out.strip())
            return False
        return True


class StateManager:
    """Maintains persistent state of managed DHCP records in JSON format."""

    def __init__(self, state_file_path: str):
        self.state_file_path = state_file_path
        self.state: Dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        if os.path.isfile(self.state_file_path):
            try:
                with open(self.state_file_path, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
                logger.debug("Loaded %d records from state file %s", len(self.state), self.state_file_path)
            except Exception as e:
                logger.error("Failed to read state file %s: %s", self.state_file_path, e)
                self.state = {}
        else:
            self.state = {}

    def save(self) -> None:
        state_dir = os.path.dirname(self.state_file_path)
        if state_dir and not os.path.exists(state_dir):
            os.makedirs(state_dir, exist_ok=True)

        temp_file = f"{self.state_file_path}.tmp.{os.getpid()}"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, sort_keys=True)
            os.replace(temp_file, self.state_file_path)
            logger.debug("Successfully persisted state to %s", self.state_file_path)
        except Exception as e:
            logger.error("Failed to save state file %s: %s", self.state_file_path, e)
            if os.path.exists(temp_file):
                os.remove(temp_file)

    def record_lease(self, ip_str: str, mac: str, hostname: str, lease_time_str: str) -> None:
        self.state[ip_str] = {
            "mac": mac,
            "hostname": hostname,
            "lease_time": lease_time_str,
            "last_synced": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    def remove_lease(self, ip_str: str) -> Optional[dict]:
        return self.state.pop(ip_str, None)

    def get_lease(self, ip_str: str) -> Optional[dict]:
        return self.state.get(ip_str)


class BuffaloDHCPSyncApp:
    """Orchestrates lease collection, filtering, hostname resolution, and DNS synchronization."""

    def __init__(self, config_path: str, dry_run: bool = False, input_file: Optional[str] = None):
        self.config_path = config_path
        self.dry_run = dry_run
        self.input_file = input_file

        self.config = configparser.ConfigParser()
        self._load_config()

        # Switch parameters
        self.switch_host = self.config.get("switch", "host", fallback="192.168.1.1")
        self.switch_port = self.config.getint("switch", "port", fallback=23)
        self.switch_user = self.config.get("switch", "user", fallback="admin")
        self.switch_pass = self.config.get("switch", "password", fallback="")
        self.switch_enable_pass = self.config.get("switch", "enable_password", fallback="")
        self.switch_prompt = self.config.get("switch", "prompt", fallback="BS-G3024MR")
        self.switch_timeout = self.config.getint("switch", "timeout", fallback=15)
        self.expect_script = self.config.get(
            "switch",
            "expect_script",
            fallback=os.path.join(os.path.dirname(os.path.abspath(__file__)), "buffalo_fetch_dhcp.exp"),
        )

        # Samba parameters
        self.samba_server = self.config.get("samba", "server", fallback="localhost")
        self.domain_zone = self.config.get("samba", "domain_zone", fallback="example.com")
        self.auth_method = self.config.get("samba", "auth_method", fallback="machine_account")
        self.samba_user = self.config.get("samba", "user", fallback="Administrator")
        self.samba_pass = self.config.get("samba", "password", fallback="")
        self.keytab_path = self.config.get("samba", "keytab_path", fallback="/etc/krb5.keytab")
        self.krb5_principal = self.config.get("samba", "krb5_principal", fallback="")
        self.sync_ptr = self.config.getboolean("samba", "sync_ptr", fallback=False)

        # Reverse zones mapping
        self.reverse_zones: Dict[str, str] = {}
        if self.config.has_section("reverse_zones"):
            for subnet, zone in self.config.items("reverse_zones"):
                self.reverse_zones[subnet.strip()] = zone.strip()

        # Operational paths
        self.state_file = self.config.get("storage", "state_file", fallback="/var/lib/buffalo-dhcp-dns/state.json")
        self.lock_file = self.config.get("storage", "lock_file", fallback="/run/lock/buffalo-dhcp-dns.lock")

        # VLAN pools
        self.pools: List[IPRangePool] = self._load_ip_pools()

        # Hostname resolution
        hosts_map_file = self.config.get("hostname", "hosts_map_file", fallback="")
        enable_netbios = self.config.getboolean("hostname", "enable_netbios", fallback=False)
        template = self.config.get("hostname", "template", fallback="dhcp-{mac_clean}")
        self.resolver = HostnameResolver(
            map_file_path=hosts_map_file if hosts_map_file else None,
            enable_netbios=enable_netbios,
            template=template,
        )

        # DNS Manager
        self.dns_manager = SambaDNSManager(
            server=self.samba_server,
            domain_zone=self.domain_zone,
            auth_method=self.auth_method,
            samba_user=self.samba_user,
            samba_password=self.samba_pass,
            keytab_path=self.keytab_path if os.path.isfile(self.keytab_path) else None,
            krb5_principal=self.krb5_principal,
            sync_ptr=self.sync_ptr,
            reverse_zones=self.reverse_zones,
            dry_run=self.dry_run,
        )

        # State Manager
        self.state_manager = StateManager(self.state_file)

    def _load_config(self) -> None:
        if os.path.isfile(self.config_path):
            self.config.read(self.config_path, encoding="utf-8")
        else:
            logger.warning("Config file %s not found. Using default configurations.", self.config_path)

    def _load_ip_pools(self) -> List[IPRangePool]:
        pools: List[IPRangePool] = []
        if self.config.has_section("vlan_pools"):
            for pool_name, range_str in self.config.items("vlan_pools"):
                range_clean = range_str.strip()
                if "-" in range_clean:
                    start_str, end_str = [x.strip() for x in range_clean.split("-", 1)]
                    try:
                        pools.append(IPRangePool(pool_name, start_str, end_str))
                    except ValueError as e:
                        logger.error("Invalid range for pool %s (%s): %s", pool_name, range_clean, e)
                elif "/" in range_clean:
                    try:
                        net = ipaddress.IPv4Network(range_clean, strict=False)
                        pools.append(IPRangePool(pool_name, str(net.network_address + 1), str(net.broadcast_address - 1)))
                    except ValueError as e:
                        logger.error("Invalid CIDR for pool %s (%s): %s", pool_name, range_clean, e)
        else:
            # Default to the 3 pools specified in the prompt:
            # VLAN 20: 192.168.20.100 - 192.168.20.200
            # VLAN 30: 192.168.30.100 - 192.168.30.200
            # VLAN 100: 192.168.100.100 - 192.168.100.200
            pools = [
                IPRangePool("vlan20", "192.168.20.100", "192.168.20.200"),
                IPRangePool("vlan30", "192.168.30.100", "192.168.30.200"),
                IPRangePool("vlan100", "192.168.100.100", "192.168.100.200"),
            ]
        logger.info("Configured %d target VLAN IP pools: %s", len(pools), pools)
        return pools

    def is_ip_in_managed_pools(self, ip: ipaddress.IPv4Address) -> bool:
        """Verifies if an IP address falls within any configured target VLAN pool."""
        return any(pool.contains(ip) for pool in self.pools)

    def fetch_bindings(self) -> str:
        """Fetches raw binding table from switch Telnet or input file."""
        if self.input_file:
            logger.info("Reading DHCP bindings from input file: %s", self.input_file)
            with open(self.input_file, "r", encoding="utf-8") as f:
                return f.read()

        if not os.path.isfile(self.expect_script):
            raise FileNotFoundError(f"Expect script not found at {self.expect_script}")

        cmd = [
            self.expect_script,
            self.switch_host,
            self.switch_user,
            self.switch_pass,
            str(self.switch_port),
            self.switch_enable_pass,
            self.switch_prompt,
            str(self.switch_timeout),
        ]
        logger.info("Connecting to BS-G3024MR at %s:%d via Telnet...", self.switch_host, self.switch_port)
        env = os.environ.copy()
        if logger.isEnabledFor(logging.DEBUG):
            env["SWITCH_DEBUG"] = "1"

        res = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=self.switch_timeout + 15,
        )
        if res.stderr:
            for err_line in res.stderr.splitlines():
                if err_line.startswith("[EXPECT DEBUG]"):
                    logger.debug("%s", err_line)
                elif res.returncode != 0:
                    logger.error("%s", err_line)

        if res.returncode != 0:
            raise RuntimeError(
                f"Expect script failed with return code {res.returncode}:\n{res.stderr.strip()}"
            )
        return res.stdout

    def parse_bindings(self, raw_output: str) -> List[DHCPLease]:
        """Parses the output of 'show ip dhcp binding' into a list of DHCPLease objects."""
        logger.debug("Raw switch binding output (%d bytes):\n%s", len(raw_output), raw_output)
        leases: List[DHCPLease] = []
        for match in DHCP_LINE_PATTERN.finditer(raw_output):
            data = match.groupdict()
            lease = DHCPLease(data["ip"], data["mac"], data["leasetime"])
            leases.append(lease)

        logger.info("Extracted %d DHCP lease entries from switch output.", len(leases))
        if len(leases) == 0:
            if len(raw_output.strip()) > 0:
                logger.warning(
                    "0 DHCP leases extracted from switch output. Raw buffer was:\n%s",
                    raw_output.strip(),
                )
            else:
                logger.warning("Switch returned an empty response buffer for 'show ip dhcp binding'.")
        return leases

    def sync(self) -> Tuple[int, int, int]:
        """
        Executes synchronization logic:
          - Filters leases by managed VLAN pools
          - Identifies new additions, updates, and removals
          - Applies changes to Samba DNS
          - Updates state file
        Returns (added_count, updated_count, deleted_count).
        """
        raw_output = self.fetch_bindings()
        all_leases = self.parse_bindings(raw_output)

        # 1. Filter leases by managed VLAN pools
        managed_leases: Dict[str, DHCPLease] = {}
        for lease in all_leases:
            if self.is_ip_in_managed_pools(lease.ip):
                managed_leases[lease.ip_str] = lease
            else:
                logger.debug("Skipping IP %s (not in target VLAN pools)", lease.ip_str)

        logger.info(
            "%d of %d leases fall within target VLAN pools.", len(managed_leases), len(all_leases)
        )

        current_state = self.state_manager.state
        added_count = 0
        updated_count = 0
        deleted_count = 0

        # Track active IPs in this sync cycle
        active_ips: Set[str] = set(managed_leases.keys())

        # 2. Check for newly added or updated leases
        for ip_str, lease in managed_leases.items():
            desired_hostname = self.resolver.resolve(lease)
            prev_record = current_state.get(ip_str)

            if prev_record is None:
                # New lease!
                logger.info("NEW LEASE DETECTED: %s (%s) -> %s", ip_str, lease.mac, desired_hostname)
                success = self.dns_manager.add_a_record(desired_hostname, ip_str)
                if success or self.dry_run:
                    if not self.dry_run:
                        self.state_manager.record_lease(ip_str, lease.mac, desired_hostname, lease.lease_time_str)
                    added_count += 1
            else:
                prev_hostname = prev_record.get("hostname")
                prev_mac = prev_record.get("mac")

                if prev_hostname != desired_hostname or prev_mac != lease.mac:
                    # Update lease record
                    logger.info(
                        "LEASE CHANGED for %s: (old: %s / %s) -> (new: %s / %s)",
                        ip_str,
                        prev_hostname,
                        prev_mac,
                        desired_hostname,
                        lease.mac,
                    )
                    if prev_hostname and prev_hostname != desired_hostname:
                        self.dns_manager.delete_a_record(prev_hostname, ip_str)
                    success = self.dns_manager.add_a_record(desired_hostname, ip_str)
                    if success or self.dry_run:
                        if not self.dry_run:
                            self.state_manager.record_lease(
                                ip_str, lease.mac, desired_hostname, lease.lease_time_str
                            )
                        updated_count += 1
                else:
                    # Unchanged, refresh timestamp in state
                    if not self.dry_run:
                        self.state_manager.record_lease(ip_str, lease.mac, desired_hostname, lease.lease_time_str)

        # 3. Check for expired or released leases (present in state, but absent in switch bindings)
        expired_ips = [
            ip_str
            for ip_str in current_state.keys()
            if ip_str not in active_ips and self.is_ip_in_managed_pools(ipaddress.IPv4Address(ip_str))
        ]

        for ip_str in expired_ips:
            record = current_state[ip_str]
            hostname = record.get("hostname")
            mac = record.get("mac")
            logger.info("LEASE EXPIRED / RELEASED: %s (%s, hostname=%s)", ip_str, mac, hostname)

            if hostname:
                self.dns_manager.delete_a_record(hostname, ip_str)

            if not self.dry_run:
                self.state_manager.remove_lease(ip_str)
            deleted_count += 1

        # 4. Save state
        if not self.dry_run:
            self.state_manager.save()
            logger.info("Saved %d active lease records to state file: %s", len(self.state_manager.state), self.state_file)
        else:
            logger.info("[DRY-RUN] Simulated state contains %d records. (state.json was NOT modified because --dry-run is active)", len(managed_leases))

        logger.info(
            "Sync cycle finished: %d added, %d updated, %d deleted.",
            added_count,
            updated_count,
            deleted_count,
        )
        return added_count, updated_count, deleted_count


def setup_logging(verbose: bool = False, log_file: Optional[str] = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        try:
            log_dir = os.path.dirname(log_file)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            file_handler.setLevel(level)
            root_logger.addHandler(file_handler)
        except Exception as e:
            logger.warning("Could not open log file %s: %s", log_file, e)


def acquire_lock(lock_file_path: str):
    lock_dir = os.path.dirname(lock_file_path)
    if lock_dir and not os.path.exists(lock_dir):
        os.makedirs(lock_dir, exist_ok=True)

    f = open(lock_file_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except (BlockingIOError, IOError):
        logger.warning("Another instance of the sync daemon is currently running. Exiting.")
        sys.exit(0)


def main():
    parser = argparse.ArgumentParser(
        description="Synchronize BUFFALO BS-G3024MR DHCP leases to Samba AD DNS."
    )
    parser.add_argument(
        "-c",
        "--config",
        default="/etc/buffalo-dhcp-dns/config.ini",
        help="Path to configuration file (default: /etc/buffalo-dhcp-dns/config.ini)",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Simulate DNS modifications without executing write commands.",
    )
    parser.add_argument(
        "-i",
        "--input-file",
        help="Read raw 'show ip dhcp binding' output from file instead of connecting to switch.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose debug logging.",
    )
    parser.add_argument(
        "--list-state",
        action="store_true",
        help="Display currently managed DHCP lease state and exit.",
    )

    args = parser.parse_args()

    # Determine default config path fallback if not found in /etc
    config_file = args.config
    if not os.path.isfile(config_file):
        local_cfg = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "config", "config.ini"
        )
        if os.path.isfile(local_cfg):
            config_file = local_cfg

    setup_logging(verbose=args.verbose)

    if args.list_state:
        app = BuffaloDHCPSyncApp(config_file, dry_run=True)
        print(f"Current Managed State File: {app.state_file}")
        print(json.dumps(app.state_manager.state, indent=2))
        return

    # Acquire lock file to prevent overlapping runs
    # Default fallback lock path in /tmp if /run/lock is not writable
    lock_path = "/run/lock/buffalo-dhcp-dns.lock"
    if not os.access("/run/lock", os.W_OK) and not os.path.exists("/run/lock"):
        lock_path = "/tmp/buffalo-dhcp-dns.lock"

    lock_handle = acquire_lock(lock_path)

    try:
        app = BuffaloDHCPSyncApp(
            config_path=config_file,
            dry_run=args.dry_run,
            input_file=args.input_file,
        )
        added, updated, deleted = app.sync()
        if args.dry_run:
            logger.info("[DRY-RUN COMPLETE] %d to add, %d to update, %d to delete.", added, updated, deleted)
    except Exception as e:
        logger.exception("Synchronization job terminated with error: %s", e)
        sys.exit(1)
    finally:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
            lock_handle.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

