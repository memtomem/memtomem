"""Which addresses outbound HTTP may connect to (#2728).

One classifier for every egress path — webhook delivery and ``mem_fetch`` —
so the two cannot drift. Stdlib only: the server imports this at startup.

The list is the contract. ``ipaddress`` flags are OR-ed in as a floor, but
their meaning moves between Python versions and some ranges never set one:
``100.64.0.0/10`` (carrier-grade NAT) sets nothing on 3.12 and 3.13, and
``fec0::/10`` (deprecated site-local) sets only ``is_site_local``.
"""

from __future__ import annotations

import ipaddress

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(spec)
    for spec in (
        "0.0.0.0/8",  # "this" network
        "10.0.0.0/8",
        "100.64.0.0/10",  # carrier-grade NAT
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local, cloud metadata endpoints
        "172.16.0.0/12",
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # TEST-NET-1
        "192.168.0.0/16",
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, incl. broadcast
        "::/128",
        "::1/128",
        "100::/64",  # discard-only
        "2001::/32",  # Teredo tunnels
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4 tunnels
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
        "fec0::/10",  # site-local (deprecated, still routed on old networks)
        "ff00::/8",  # multicast
    )
)

_NAT64 = ipaddress.ip_network("64:ff9b::/96")

_BLOCKING_FLAGS = (
    "is_private",
    "is_loopback",
    "is_link_local",
    "is_multicast",
    "is_reserved",
    "is_unspecified",
    "is_site_local",
)


def _carried_ipv4(addr: IPAddress) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv4-mapped or NAT64 address carries, if any.

    Both reach an IPv4 host, and the embedded address is what the connection
    lands on, so that is what gets classified. The carrier prefix itself is
    not put through the ``ipaddress`` flags: whether ``::ffff:0:0/96`` is
    ``is_reserved`` differs between CPython patch releases, and a policy
    must not. (6to4 and Teredo also carry an IPv4 address, but those are
    tunnels into networks this process cannot vouch for, so their prefixes
    are blocked outright in the list above.)
    """
    if not isinstance(addr, ipaddress.IPv6Address):
        return None
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    if addr in _NAT64:
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    return None


def _in_blocked_network(addr: IPAddress) -> str | None:
    for net in _BLOCKED_NETWORKS:
        if addr.version == net.version and addr in net:
            return f"in {net}"
    return None


def _reason_for(addr: IPAddress) -> str | None:
    reason = _in_blocked_network(addr)
    if reason is not None:
        return reason
    for flag in _BLOCKING_FLAGS:
        if getattr(addr, flag, False):
            return flag
    return None


def blocked_address_reason(address: str | IPAddress) -> str | None:
    """Why outbound HTTP must not connect to ``address``, or ``None``.

    ``address`` is an IP literal or a parsed address; a zone index
    (``fe80::1%eth0``) is dropped before parsing. The reason names the
    network or flag that matched, for the log line.
    """
    if isinstance(address, str):
        addr: IPAddress = ipaddress.ip_address(address.split("%", 1)[0])
    else:
        addr = address
    carried = _carried_ipv4(addr)
    if carried is not None:
        reason = _in_blocked_network(addr)
        if reason is not None:
            return reason
        inner = _reason_for(carried)
        if inner is not None:
            return f"carries {carried} ({inner})"
        return None
    return _reason_for(addr)
