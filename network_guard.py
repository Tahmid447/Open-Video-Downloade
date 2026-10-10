"""Guard Python sockets at the actual connection, including redirected URLs.
The VPS bridge firewall adds an OS boundary for native helpers/subprocesses.
"""
import ipaddress
import socket
import sys


def public_destination(address):
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def install():
    original = socket.getaddrinfo
    def guarded_resolution(host, port, *args, **kwargs):
        results = original(host, port, *args, **kwargs)
        if any(not public_destination(item[4][0]) for item in results):
            raise OSError("Private network destinations are blocked.")
        return results
    socket.getaddrinfo = guarded_resolution
    def audit(event, args):
        if event not in {"socket.connect", "socket.sendto"}:
            return
        sock = args[0]
        address = args[1]
        if sock.family not in {socket.AF_INET, socket.AF_INET6}:
            raise OSError("Non-web sockets are blocked.")
        host, port = address[:2]
        if port not in {80, 443}:
            raise OSError("Only standard web ports are supported.")
        try:
            allowed = public_destination(host)
        except ValueError:
            # Libraries normally pass resolved IPs. Validate hostnames too.
            allowed = all(public_destination(item[4][0]) for item in guarded_resolution(host, port))
        if not allowed:
            raise OSError("Private network destinations are blocked.")
    sys.addaudithook(audit)
