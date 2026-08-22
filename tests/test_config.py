from __future__ import annotations

import pytest

from unifi_netbox_sync.config import Settings

REQUIRED_ENV = {
    "UNIFI_HOST": "https://192.168.1.1",
    "NETBOX_URL": "https://netbox.example.com",
    "NETBOX_TOKEN": "token",
    "NETBOX_SITE_SLUG": "main",
}


def test_from_env_accepts_username_and_password(monkeypatch):
    monkeypatch.setenv("UNIFI_USERNAME", "admin")
    monkeypatch.setenv("UNIFI_PASSWORD", "secret")
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    settings = Settings.from_env()

    assert settings.unifi_api_key is None
    assert settings.unifi_username == "admin"


def test_from_env_accepts_api_key_without_password(monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "secret-key")
    monkeypatch.delenv("UNIFI_USERNAME", raising=False)
    monkeypatch.delenv("UNIFI_PASSWORD", raising=False)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    settings = Settings.from_env()

    assert settings.unifi_api_key == "secret-key"
    assert settings.unifi_username == ""


def test_from_env_requires_some_auth_method(monkeypatch):
    monkeypatch.delenv("UNIFI_API_KEY", raising=False)
    monkeypatch.delenv("UNIFI_USERNAME", raising=False)
    monkeypatch.delenv("UNIFI_PASSWORD", raising=False)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(SystemExit):
        Settings.from_env()
