from __future__ import annotations

import pytest
import responses

from unifi_netbox_sync.unifi_client import UnifiClientAPI


@responses.activate
def test_login_and_get_clients_udm():
    host = "https://udm.example.com"
    responses.add(responses.POST, f"{host}/api/auth/login", json={}, status=200)
    responses.add(responses.POST, f"{host}/api/auth/logout", json={}, status=200)
    responses.add(
        responses.GET,
        f"{host}/proxy/network/api/s/default/stat/sta",
        json={
            "data": [
                {
                    "mac": "AA:BB:CC:DD:EE:01",
                    "name": "Laptop",
                    "ip": "10.0.0.5",
                    "is_wired": True,
                    "sw_mac": "11:22:33:44:55:66",
                    "sw_port": 4,
                },
                {
                    "mac": "AA:BB:CC:DD:EE:02",
                    "hostname": "phone",
                    "ip": "10.0.0.6",
                    "is_wired": False,
                    "ap_mac": "22:33:44:55:66:77",
                    "essid": "HomeWiFi",
                },
            ]
        },
        status=200,
    )

    client = UnifiClientAPI(host=host, username="admin", password="secret", is_udm=True)
    with client:
        clients = client.get_clients("default")

    assert len(clients) == 2
    wired = clients[0]
    assert wired.mac == "aa:bb:cc:dd:ee:01"
    assert wired.is_wired is True
    assert wired.switch_mac == "11:22:33:44:55:66"
    assert wired.switch_port == 4
    assert wired.ap_mac is None

    wireless = clients[1]
    assert wireless.mac == "aa:bb:cc:dd:ee:02"
    assert wireless.name == "phone"
    assert wireless.is_wired is False
    assert wireless.essid == "HomeWiFi"
    assert wireless.switch_mac is None


@responses.activate
def test_classic_controller_uses_plain_api_prefix():
    host = "https://controller.example.com:8443"
    responses.add(responses.POST, f"{host}/api/login", json={}, status=200)
    responses.add(responses.POST, f"{host}/api/logout", json={}, status=200)
    responses.add(
        responses.GET,
        f"{host}/api/s/default/stat/device",
        json={"data": [{"mac": "aa:bb:cc:00:00:01", "name": "core-switch", "model": "USW-24", "type": "usw"}]},
        status=200,
    )

    client = UnifiClientAPI(host=host, username="admin", password="secret", is_udm=False)
    with client:
        devices = client.get_devices("default")

    assert len(devices) == 1
    assert devices[0].is_switch is True
    assert devices[0].name == "core-switch"


@responses.activate
def test_expired_session_triggers_relogin():
    host = "https://udm.example.com"
    responses.add(responses.POST, f"{host}/api/auth/login", json={}, status=200)
    responses.add(responses.POST, f"{host}/api/auth/logout", json={}, status=200)
    responses.add(
        responses.GET,
        f"{host}/proxy/network/api/s/default/stat/sta",
        json={},
        status=401,
    )
    responses.add(
        responses.GET,
        f"{host}/proxy/network/api/s/default/stat/sta",
        json={"data": []},
        status=200,
    )

    client = UnifiClientAPI(host=host, username="admin", password="secret", is_udm=True)
    with client:
        clients = client.get_clients("default")

    assert clients == []
    # initial login, first GET (401), re-login, retried GET (200), logout on context exit
    assert len(responses.calls) == 5


@responses.activate
def test_api_key_auth_skips_login_and_sets_header():
    host = "https://udm.example.com"
    responses.add(
        responses.GET,
        f"{host}/proxy/network/api/s/default/stat/sta",
        json={"data": []},
        status=200,
    )

    client = UnifiClientAPI(host=host, api_key="secret-key", is_udm=True)
    with client:
        clients = client.get_clients("default")

    assert clients == []
    # No login/logout HTTP calls at all with API-key auth: just the one GET.
    assert len(responses.calls) == 1
    assert responses.calls[0].request.headers["X-API-KEY"] == "secret-key"


@responses.activate
def test_api_key_auth_does_not_retry_on_401():
    host = "https://udm.example.com"
    responses.add(
        responses.GET,
        f"{host}/proxy/network/api/s/default/stat/sta",
        json={},
        status=401,
    )

    client = UnifiClientAPI(host=host, api_key="bad-key", is_udm=True)
    with client, pytest.raises(Exception):
        client.get_clients("default")

    # A single failed GET, no re-login attempt (there's no session to refresh).
    assert len(responses.calls) == 1


def test_requires_api_key_or_username_and_password():
    with pytest.raises(ValueError):
        UnifiClientAPI(host="https://udm.example.com")


@responses.activate
def test_client_oui_and_device_serial_are_captured():
    """The controller already resolves each client's vendor from its MAC OUI;
    capture it rather than shipping our own database."""
    host = "https://udm.example.com"
    responses.add(responses.POST, f"{host}/api/auth/login", json={}, status=200)
    responses.add(responses.POST, f"{host}/api/auth/logout", json={}, status=200)
    responses.add(
        responses.GET,
        f"{host}/proxy/network/api/s/default/stat/sta",
        json={
            "data": [
                {
                    "mac": "00:1B:63:AA:BB:CC",
                    "hostname": "macbook",
                    "ip": "10.0.0.5",
                    "is_wired": True,
                    "oui": "Apple, Inc.",
                    "sw_mac": "11:22:33:44:55:66",
                    "sw_port": 7,
                },
                {"mac": "02:00:00:00:00:01", "is_wired": False},  # no oui reported
            ]
        },
        status=200,
    )
    responses.add(
        responses.GET,
        f"{host}/proxy/network/api/s/default/stat/device",
        json={
            "data": [
                {
                    "mac": "11:22:33:44:55:66",
                    "name": "core-switch",
                    "model": "USW-24",
                    "type": "usw",
                    "serial": "F09FC2ABCDEF",
                }
            ]
        },
        status=200,
    )

    client = UnifiClientAPI(host=host, api_key="k", is_udm=True)
    with client:
        clients = client.get_clients("default")
        devices = client.get_devices("default")

    assert clients[0].oui == "Apple, Inc."
    assert clients[1].oui is None
    # Serial is one of the fallbacks used to find the switch in NetBox.
    assert devices[0].serial == "F09FC2ABCDEF"
    assert devices[0].name == "core-switch"
