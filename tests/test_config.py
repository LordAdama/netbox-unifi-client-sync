from __future__ import annotations

import pytest

from unifi_netbox_sync.config import Settings

REQUIRED_ENV = {
    "UNIFI_HOST": "https://192.168.1.1",
    "NETBOX_URL": "https://netbox.example.com",
    "NETBOX_TOKEN": "token",
    "NETBOX_SITE_SLUG": "main",
}


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    # from_env() reads a .env from the current directory; run every test in
    # an empty directory so a developer's real local .env can't leak in.
    monkeypatch.chdir(tmp_path)


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


def test_from_env_accepts_site_map_instead_of_site_slug(monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "secret-key")
    monkeypatch.setenv("UNIFI_HOST", REQUIRED_ENV["UNIFI_HOST"])
    monkeypatch.setenv("NETBOX_URL", REQUIRED_ENV["NETBOX_URL"])
    monkeypatch.setenv("NETBOX_TOKEN", REQUIRED_ENV["NETBOX_TOKEN"])
    monkeypatch.delenv("NETBOX_SITE_SLUG", raising=False)
    monkeypatch.setenv("SITE_MAP", "hq:headquarters, branch:branch-office")

    settings = Settings.from_env()

    assert settings.site_map == {"hq": "headquarters", "branch": "branch-office"}
    pairs = {(p.unifi_site, p.netbox_site_slug) for p in settings.site_pairs()}
    assert pairs == {("hq", "headquarters"), ("branch", "branch-office")}


def test_from_env_rejects_malformed_site_map_entry(monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "secret-key")
    monkeypatch.setenv("UNIFI_HOST", REQUIRED_ENV["UNIFI_HOST"])
    monkeypatch.setenv("NETBOX_URL", REQUIRED_ENV["NETBOX_URL"])
    monkeypatch.setenv("NETBOX_TOKEN", REQUIRED_ENV["NETBOX_TOKEN"])
    monkeypatch.setenv("SITE_MAP", "not-a-valid-entry")

    with pytest.raises(SystemExit):
        Settings.from_env()


def test_from_env_requires_site_slug_or_site_map(monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "secret-key")
    monkeypatch.setenv("UNIFI_HOST", REQUIRED_ENV["UNIFI_HOST"])
    monkeypatch.setenv("NETBOX_URL", REQUIRED_ENV["NETBOX_URL"])
    monkeypatch.setenv("NETBOX_TOKEN", REQUIRED_ENV["NETBOX_TOKEN"])
    monkeypatch.delenv("NETBOX_SITE_SLUG", raising=False)
    monkeypatch.delenv("SITE_MAP", raising=False)

    with pytest.raises(SystemExit):
        Settings.from_env()


def test_from_env_loads_dotenv_file_without_shell_parsing(tmp_path, monkeypatch):
    # Values with spaces/commas would break a bash `source .env` — confirm
    # our own loader (not a shell) handles them correctly.
    (tmp_path / ".env").write_text(
        "UNIFI_HOST=https://192.168.1.1\n"
        "UNIFI_API_KEY=secret-key\n"
        "NETBOX_URL=https://netbox.example.com\n"
        "NETBOX_TOKEN=token\n"
        "NETBOX_SITE_SLUG=main\n"
        "PORT_NAME_TEMPLATES={port},Port {port},GE{port},Gi{port}\n"
        "SITE_MAP=hq:headquarters,branch:branch-office\n"
        "# a comment line, and a blank line follow\n"
        "\n"
        'QUOTED_VALUE="quoted"\n'
    )
    for key in ("UNIFI_HOST", "UNIFI_API_KEY", "NETBOX_URL", "NETBOX_TOKEN", "NETBOX_SITE_SLUG", "SITE_MAP"):
        monkeypatch.delenv(key, raising=False)

    settings = Settings.from_env()

    assert settings.unifi_host == "https://192.168.1.1"
    assert settings.port_name_templates == ["{port}", "Port {port}", "GE{port}", "Gi{port}"]
    assert settings.site_map == {"hq": "headquarters", "branch": "branch-office"}


def test_from_env_prefers_real_environment_over_dotenv_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("UNIFI_HOST=https://from-dotenv\n")
    monkeypatch.setenv("UNIFI_HOST", "https://from-real-env")
    monkeypatch.setenv("UNIFI_API_KEY", "secret-key")
    monkeypatch.setenv("NETBOX_URL", REQUIRED_ENV["NETBOX_URL"])
    monkeypatch.setenv("NETBOX_TOKEN", REQUIRED_ENV["NETBOX_TOKEN"])
    monkeypatch.setenv("NETBOX_SITE_SLUG", REQUIRED_ENV["NETBOX_SITE_SLUG"])

    settings = Settings.from_env()

    assert settings.unifi_host == "https://from-real-env"
