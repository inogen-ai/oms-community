"""The address guard for every outbound request to GitHub, shared by the git reader and the REST API reader.

Every resolved address must be a global one, and the lowest is the only one used, so a
resolver that answers with a private address cannot steer a request inside the network
and a second lookup cannot rebind it.
"""
import ipaddress


class UnsafeDestination(ValueError):
    """Nothing resolved, or a resolved address is not a global one."""


def pinned_address(addresses: list[str]) -> str:
    """The one address to connect to: every address must be global; the lowest wins, IPv6 in brackets."""
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeDestination("unsafe network destination")
    address = sorted(set(addresses))[0]
    return f"[{address}]" if ":" in address else address
