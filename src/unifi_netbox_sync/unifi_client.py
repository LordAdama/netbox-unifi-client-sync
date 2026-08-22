from __future__ import annotations

import logging

import requests
import urllib3

from .models import UnifiClient, UnifiSwitchDevice

logger = logging.getLogger(__name__)


class UnifiAuthError(RuntimeError):
    pass


class UnifiClientAPI:
    """Minimal client for the UniFi Network Controller REST API.

    Supports both the classic self-hosted controller and UniFi OS
    (UDM/UDM-Pro/CloudKey Gen2+), which mounts the network application
    under an extra "/proxy/network" path prefix.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        is_udm: bool = True,
        verify_ssl: bool = False,
        timeout: float = 15.0,
    ) -> None:
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.is_udm = is_udm
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_ssl
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._api_prefix = "/proxy/network/api" if is_udm else "/api"
        self._authenticated = False

    def _login_url(self) -> str:
        return f"{self.host}{'/api/auth/login' if self.is_udm else '/api/login'}"

    def login(self) -> None:
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
        if not self._authenticated:
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
        if resp.status_code == 401:
            # Session likely expired; retry once after re-authenticating.
            self.login()
            resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        payload = resp.json()
        return payload.get("data", [])

    def get_sites(self) -> list[dict]:
        return self._get("/self/sites")

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
                )
            )
        return clients
