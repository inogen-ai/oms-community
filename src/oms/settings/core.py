"""Local storage, publication and network settings for the core product."""
from dataclasses import dataclass, field, replace
import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

from oms.web.endpoints import contribution_endpoint_for, mcp_endpoint_for

# The single-machine posture, and the common one: the server, the agents and
# the person are all on this laptop, so the loopback address it already serves
# on is the address a published bundle should name.
DEFAULT_PUBLIC_URL = "http://localhost:4317"


GIT_LOOPBACK_REFUSAL = (
    "the address published bundles advertise is a loopback address, so this "
    "bundle would tell every machine that clones the repository to post its "
    "corrections to itself, where nothing is listening. Set OMS_PUBLIC_URL to "
    "the address this server is reachable on, then publish again.")


def _public_url(port: int) -> str | None:
    """`OMS_PUBLIC_URL` resolved.

      unset        -> the loopback default for this port
      set, value   -> that base URL, trailing slash removed
      set, empty   -> None: contribution off, no front door advertised anywhere

    An empty value is the documented off switch, not a malformed URL, so it is
    honoured before anything is validated.
    """
    raw = os.environ.get("OMS_PUBLIC_URL")
    if raw is None:
        return f"http://localhost:{port}"
    return raw.strip().rstrip("/") or None


def advertises_loopback(public_url: str | None) -> bool:
    """True when the advertised address only answers on the machine serving it.

    Read from the URL rather than from how it was configured. A deployment can
    arrive at loopback three ways - the setting left unset, a launcher passing
    the same default in, an operator typing it - and all three break a bundle
    that travels to another machine. Contribution turned off (None) is not
    loopback: that bundle advertises nothing and loses nothing.
    """
    if not public_url:
        return False
    host = urlsplit(public_url).hostname
    if host is None:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class CoreSettings:
    tenant_id: str = "acme"
    data_dir: Path = field(default_factory=lambda: Path(".oms"))
    host: str = "127.0.0.1"
    port: int = 4317
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "change-me"
    allowed_origins: tuple[str, ...] = (
        "http://localhost:4317", "http://127.0.0.1:4317",
        "http://localhost:4318", "http://127.0.0.1:4318")
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")
    acknowledge_network_exposure: bool = False
    # The base URL a published bundle tells agents to call back on. None turns
    # contribution off: no instruction in the root files, no bundled
    # `oms_contribute.py`, no MCP configuration. The default names the default
    # port; `with_port` carries it when the server is moved.
    public_url: str | None = DEFAULT_PUBLIC_URL
    ui_dir: Path | None = None
    publish_root: Path | None = None
    publish_host_root: str | None = None
    publish_host_home: str | None = None
    local_launcher_dir: str | None = None
    compose_project_name: str | None = None
    publish_in_container: bool = False

    def validate(self):
        if not self.tenant_id.strip():
            raise ValueError("workspace identifier is required")
        if type(self.port) is not int or not 0 < self.port < 65536:
            raise ValueError("invalid HTTP port")
        try:
            local = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            local = self.host == "localhost"
        permissive = "*" in self.allowed_origins or "*" in self.allowed_hosts
        for allowed_host in self.allowed_hosts:
            try:
                host_local = ipaddress.ip_address(allowed_host).is_loopback
            except ValueError:
                host_local = allowed_host == "localhost"
            permissive |= not host_local
        for origin in self.allowed_origins:
            if origin == "*":
                continue
            parsed = urlsplit(origin)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.path or parsed.query or parsed.fragment or parsed.username):
                raise ValueError("allowed origins must be exact HTTP origins")
            try:
                origin_local = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                origin_local = parsed.hostname == "localhost"
            permissive |= not origin_local
        if (not local or permissive) and not self.acknowledge_network_exposure:
            raise ValueError("network exposure requires OMS_ACKNOWLEDGE_NETWORK_EXPOSURE=1")
        if not self.allowed_hosts or not self.allowed_origins:
            raise ValueError("explicit allowed hosts and browser origins are required")
        if self.public_url is not None:
            parsed = urlsplit(self.public_url)
            # A path prefix is allowed, unlike a browser origin: this is a base
            # URL and a deployment behind a path-routing proxy has one. A
            # missing scheme is how it goes wrong, because a hosting panel
            # prints the hostname alone and pasting that ships a bundle whose
            # every front door is unusable.
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                suggestion = (self.public_url if "://" in self.public_url
                              else f"https://{self.public_url}")
                raise ValueError(
                    f"OMS_PUBLIC_URL={self.public_url!r} is not an absolute http(s) "
                    f"address. Every published front door is built from it, so a "
                    f"value without a scheme ships a bundle nobody can contribute "
                    f"through. Did you mean {suggestion.rstrip('/')!r}?")
            if parsed.query or parsed.fragment:
                # The route is concatenated onto this value, so a query or a
                # fragment lands in front of `/api/ingest` and the address is
                # unusable wherever it is read.
                raise ValueError(
                    "OMS_PUBLIC_URL must be a base address with no query string "
                    "and no fragment; the ingest and MCP paths are appended to it.")
            if parsed.username or parsed.password:
                # This value is written verbatim into oms_contribute.py,
                # .mcp.json, .cursor/mcp.json, both installers and the bundle
                # README, and those are pushed to the skills repository.
                raise ValueError(
                    "OMS_PUBLIC_URL must not carry credentials: it is written into "
                    "published files that are copied to every machine and may be "
                    "pushed to a shared repository.")
        return self

    def with_port(self, port: int) -> "CoreSettings":
        """Move the HTTP port, carrying the advertised address with it when that
        address is this server's own loopback default.

        `oms serve --port` moves where the server listens. A published bundle
        left naming the old port sends every agent to a closed door, and the
        operator sees nothing: the publish succeeds, and the failure happens
        later, on the machine that installed the bundle.
        """
        if port == self.port:
            return self
        moved = replace(self, port=port)
        if self.public_url == f"http://localhost:{self.port}":
            return replace(moved, public_url=f"http://localhost:{port}")
        return moved

    @property
    def contribution_endpoint(self) -> str | None:
        """Where published agents are told to POST, derived from the same
        constant as the mounted route so the two cannot drift apart."""
        return contribution_endpoint_for(self.public_url)

    @property
    def mcp_endpoint(self) -> str | None:
        return mcp_endpoint_for(self.public_url)

    @classmethod
    def from_env(cls):
        defaults = cls()
        def values(name, fallback):
            return tuple(v.strip() for v in os.environ[name].split(",") if v.strip()) if name in os.environ else fallback
        port = int(os.environ.get("OMS_PORT", defaults.port))
        return cls(
            tenant_id=os.environ.get("OMS_TENANT", defaults.tenant_id),
            data_dir=Path(os.environ.get("OMS_DATA_DIR", ".oms")),
            host=os.environ.get("OMS_HOST", defaults.host),
            port=port,
            public_url=_public_url(port),
            neo4j_uri=os.environ.get("OMS_NEO4J_URI", defaults.neo4j_uri),
            neo4j_user=os.environ.get("OMS_NEO4J_USER", defaults.neo4j_user),
            neo4j_password=os.environ.get("OMS_NEO4J_PASSWORD", defaults.neo4j_password),
            allowed_origins=values("OMS_ALLOWED_ORIGINS", defaults.allowed_origins),
            allowed_hosts=values("OMS_ALLOWED_HOSTS", defaults.allowed_hosts),
            acknowledge_network_exposure=os.environ.get("OMS_ACKNOWLEDGE_NETWORK_EXPOSURE") == "1",
            ui_dir=Path(os.environ["OMS_UI_DIR"]) if os.environ.get("OMS_UI_DIR") else None,
            publish_root=Path(os.environ["OMS_PUBLISH_ROOT"]) if os.environ.get("OMS_PUBLISH_ROOT") else None,
            publish_host_root=os.environ.get("OMS_PUBLISH_HOST_ROOT") or None,
            publish_host_home=os.environ.get("OMS_PUBLISH_HOST_HOME") or None,
            local_launcher_dir=os.environ.get("OMS_LOCAL_LAUNCHER_DIR") or None,
            compose_project_name=os.environ.get("OMS_COMPOSE_PROJECT_NAME") or None,
            publish_in_container=os.environ.get("OMS_PUBLISH_IN_CONTAINER") == "1",
        ).validate()
