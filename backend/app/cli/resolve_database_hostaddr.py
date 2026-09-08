from __future__ import annotations

import argparse
import ipaddress
import socket
import sys
from urllib.parse import urlsplit

from app.core.config import get_settings
from app.services.source_probe import DnsOverHttpsResolver

DEFAULT_DOH_URL = "https://dns.google/resolve"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--all",
        action="store_true",
        help="print every trusted public address as a comma-separated list",
    )
    args = parser.parse_args()
    settings = get_settings()
    hostname = urlsplit(settings.database_url).hostname
    if not hostname:
        print("database URL has no hostname", file=sys.stderr)
        return 2

    resolver = DnsOverHttpsResolver(
        settings.source_probe_doh_url or DEFAULT_DOH_URL,
        timeout_seconds=settings.source_probe_timeout_seconds,
    )
    try:
        records = resolver(hostname, 5432, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        print(f"trusted database DNS resolution failed: {exc}", file=sys.stderr)
        return 2

    addresses = public_hostaddrs(records)
    if not addresses:
        print("trusted database DNS resolution returned no public address", file=sys.stderr)
        return 2
    print(",".join(addresses) if args.all else addresses[0])
    return 0


def public_hostaddrs(records: list[tuple[object, ...]]) -> tuple[str, ...]:
    addresses: list[str] = []
    for record in records:
        try:
            address = str(record[4][0])  # type: ignore[index]
            ip = ipaddress.ip_address(address)
        except (IndexError, TypeError, ValueError):
            continue
        if ip.is_global and address not in addresses:
            addresses.append(address)
    return tuple(addresses)


if __name__ == "__main__":
    raise SystemExit(main())
