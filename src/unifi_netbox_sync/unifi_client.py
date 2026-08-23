from __future__ import annotations

import logging

import requests
import urllib3

from .models import UnifiClient, UnifiPort, UnifiSite, UnifiSwitchDevice

logger = logging.getLogger(__name__)


class UnifiAuthError(RuntimeError):
    pass


def _parse_port_table(raw: list[dict]) -> list[UnifiPort]:
    """Physical ports as the controller reports them.

    This is the authoritative port list for the actual hardware, which is why
    it is preferred over a static device-type library for building interfaces.
    """
    ports = []
    for entry in raw:
        index = entry.get("port_idx")
        if index is None:
            continue
        ports.append(
            UnifiPort(
                index=int(index),
                name=(entry.get("name") or "").strip(),
                media=(entry.get("media") or "").strip(),
                max_speed=int(entry.get("max_speed") or entry.get("speed") or 0),
                is_uplink=bool(entry.get("is_uplink", False)),
            )
        )
    return sorted(ports, key=lambda p: p.index)


class UnifiClientAPI:
    """Minimal client for the UniFi Network Controller REST API.

    Supports both the classic self-hosted controller and UniFi OS
    (UDM/UDM-Pro/CloudKey Gen2+), which mounts the network application
    under an extra "/proxy/network" path prefix.
    """

    def __init__(
        self,
        host: str,
        username: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        is_udm: bool = True,
        verify_ssl: bool = False,
        timeout: float = 15.0,
    ) -> None:
        if not api_key and not (username and password):
            raise ValueError("UnifiClientAPI requires either api_key, or both username and password")

        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.api_key = api_key
        self.is_udm = is_udm
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_ssl
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        if api_key:
            # Local UniFi OS API keys (Settings -> Control Plane -> Integrations)
            # authenticate every request via this header instead of a cookie
            # session, so there is no login/logout call and no session-expiry
            # edge case. Support varies by controller firmware version; verify
            # with --dry-run before relying on it.
            self.session.headers["X-API-KEY"] = api_key

        self._api_prefix = "/proxy/network/api" if is_udm else "/api"
        self._authenticated = False

    def _login_url(self) -> str:
        return f"{self.host}{'/api/auth/login' if self.is_udm else '/api/login'}"

    def login(self) -> None:
        if self.api_key:
            self._authenticated = True
            logger.info("Using UniFi API key authentication for %s (no session login)", self.host)
            return
        resp = self.session.post(
            self._login_url(),
            json={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise UnifiAuthError(
                f"UniFi login failed with status {resp.status_code}: {resp.text[:300]}"
            )
        self._authenticated = True
        logger.info("Authenticated to UniFi controller at %s", self.host)

    def logout(self) -> None:
        if self.api_key or not self._authenticated:
            self._authenticated = False
            return
        url = f"{self.host}{'/api/auth/logout' if self.is_udm else '/api/logout'}"
        try:
            self.session.post(url, timeout=self.timeout)
        finally:
            self._authenticated = False

    def __enter__(self) -> "UnifiClientAPI":
        self.login()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.logout()

    def _get(self, path: str) -> list[dict]:
        if not self._authenticated:
            self.login()
        url = f"{self.host}{self._api_prefix}{path}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 401 and not self.api_key:
            # Session likely expired; retry once after re-authenticating.
            # (Not applicable to API-key auth: there's no session to expire.)
            self.login()
            resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        payload = resp.json()
        return payload.get("data", [])

    def get_sites(self) -> list[UnifiSite]:
        """Every site this account can see on the controller.

        Requires an account with visibility of all sites — a per-site admin
        will only get the ones it administers, which is usually what you want
        anyway.
        """
        sites = []
        for entry in self._get("/self/sites"):
            name = entry.get("name")
            if not name:
                continue
            sites.append(UnifiSite(name=name, description=(entry.get("desc") or "").strip()))
        return sites

    def get_devices(self, site: str) -> list[UnifiSwitchDevice]:
        raw = self._get(f"/s/{site}/stat/device")
        devices = []
        for entry in raw:
            devices.append(
                UnifiSwitchDevice(
                    mac=entry.get("mac", "").lower(),
                    name=entry.get("name") or entry.get("model", "unnamed"),
                    model=entry.get("model", ""),
                    device_type=entry.get("type", ""),
                    serial=entry.get("serial") or None,
                    ip=entry.get("ip") or None,
                    version=entry.get("version") or None,
                    # Absent on older firmware; treat as adopted, since
                    # /stat/device only lists devices the controller manages.
                    adopted=bool(entry.get("adopted", True)),
                    ports=_parse_port_table(entry.get("port_table") or []),
                )
            )
        return devices

    def get_clients(self, site: str) -> list[UnifiClient]:
        raw = self._get(f"/s/{site}/stat/sta")
        clients = []
        for entry in raw:
            mac = entry.get("mac", "").lower()
            if not mac:
                continue
            is_wired = bool(entry.get("is_wired", False))
            clients.append(
                UnifiClient(
                    mac=mac,
                    name=entry.get("name") or entry.get("hostname") or "",
                    ip=entry.get("ip"),
                    is_wired=is_wired,
                    switch_mac=(entry.get("sw_mac") or "").lower() or None if is_wired else None,
                    switch_port=entry.get("sw_port") if is_wired else None,
                    ap_mac=(entry.get("ap_mac") or "").lower() or None if not is_wired else None,
                    essid=entry.get("essid") if not is_wired else None,
                    oui=(entry.get("oui") or "").strip() or None,
                )
            )
        return clients
