"""Local network discovery without optional Ubuntu packages."""
import ipaddress
import json
import pathlib
import re
import socket
import subprocess
import sys


def run(command):
    return subprocess.run(command, capture_output=True, text=True, timeout=5, check=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout


def linux_addresses():
    return json.loads(run(["ip", "-j", "-4", "address", "show", "up"]))


def detect_host(network=None):
    allowed = ipaddress.ip_network(network, strict=False) if network else None
    if sys.platform.startswith("linux"):
        try:
            candidates = []
            for interface in linux_addresses():
                name = interface["ifname"]
                physical = name.startswith(("en", "eth", "wl"))
                for address in interface.get("addr_info", []):
                    ip = ipaddress.ip_address(address["local"])
                    if address.get("scope") != "global" or ip.is_loopback or (allowed and ip not in allowed):
                        continue
                    candidates.append((not physical, not ip.is_private, name, str(ip)))
            if candidates:
                return min(candidates)[-1]
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((str(next(allowed.hosts())) if allowed else "198.51.100.1", 80))
        host = probe.getsockname()[0]
    if allowed and ipaddress.ip_address(host) not in allowed:
        raise ValueError("No LAN adapter is available in " + str(allowed))
    return host


def detect_network(host):
    if sys.platform.startswith("linux"):
        try:
            for interface in linux_addresses():
                for address in interface.get("addr_info", []):
                    if address["local"] == host:
                        return ipaddress.ip_network(host + "/" + str(address["prefixlen"]), strict=False)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            pass
    return ipaddress.ip_network(host + "/24", strict=False)


def network_neighbors(host, network):
    if sys.platform.startswith("linux"):
        try:
            text = run(["ip", "-4", "neigh", "show"])
            text = "\n".join(line for line in text.splitlines() if not any(state in line for state in ("FAILED", "INCOMPLETE")))
        except (OSError, subprocess.SubprocessError):
            text = pathlib.Path("/proc/net/arp").read_text()
    else:
        text = run(["arp", "-a"])
    addresses = set(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text))
    valid = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if address in network and value != host and address not in (network.network_address, network.broadcast_address):
            valid.append(value)
    return sorted(valid, key=ipaddress.ip_address)
