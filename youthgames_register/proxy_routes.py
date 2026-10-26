"""Explicit, fixed HTTP CONNECT route; never use environment/direct fallback."""

from dataclasses import dataclass, field
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx

from .config import signature


@dataclass(frozen=True)
class ProxyConnection:
    name: str
    proxy_url: str = field(repr=False)
    interval: float = 5.0
    public_ip: str | None = None
    source_ip: str | None = None
    dynamic_source: bool = False

    is_proxy = True

    @property
    def key(self):
        return signature({'policy': 'approved-fixed-proxy-v1', 'endpoint': self.proxy_url})

    @property
    def interface(self):
        # Existing immutable route evidence also stores a device identifier.
        # A synthetic identifier keeps proxy and physical slots independent.
        return 'proxy:' + self.key[:24]

    @property
    def label(self):
        return 'Pakistan Proxy' if self.name == 'pakistan_proxy' else self.name

    def public(self):
        return {'connection': self.name, 'label': self.label, 'interface': self.interface,
                'connection_key': self.key, 'transport': 'http_proxy',
                'dispatch_interval_seconds': self.interval}

    def transport(self):
        return httpx.AsyncHTTPTransport(
            proxy=self.proxy_url, verify=True, trust_env=False, retries=0,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=60))


def load_proxy(path):
    try:
        config = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise ValueError('Cannot read proxy config') from None
    fields = {'schema_version', 'name', 'url', 'expected_public_ip', 'dispatch_interval_seconds'}
    if not isinstance(config, dict) or set(config) != fields or type(config['schema_version']) is not int or config['schema_version'] != 1:
        raise ValueError('Use the schema-1 Pakistan proxy config')
    name = config['name']
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', name):
        raise ValueError('Proxy name must be a simple connection label')
    interval = config['dispatch_interval_seconds']
    if type(interval) not in (int, float) or not 5 <= interval <= 3600:
        raise ValueError('Proxy interval must be finite, 5–3600 seconds')
    try:
        url = config['url']
        if not isinstance(url, str) or any(c.isspace() for c in url):
            raise ValueError()
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.username is not None or parsed.password is not None
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or parsed.port is None or not 1 <= parsed.port <= 65535
                or not ipaddress.IPv4Address(parsed.hostname).is_global):
            raise ValueError()
        endpoint = f'http://{parsed.hostname}:{parsed.port}'
    except (ValueError, TypeError):
        raise ValueError('Proxy URL must be http://PUBLIC_IPV4:PORT, without credentials, paths or query strings') from None
    public = config['expected_public_ip']
    try:
        if public is not None and (not isinstance(public, str) or not ipaddress.IPv4Address(public).is_global):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Proxy expected_public_ip must be global IPv4 or null') from None
    return ProxyConnection(name, endpoint, float(interval), public)
