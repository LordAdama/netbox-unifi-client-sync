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
    for key in ("DRY_RUN", "DEVICE_UPDATE_POLICY", "SITE_POLICY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    settings = Settings.from_env()

    assert settings.unifi_api_key is None
    assert settings.unifi_username == "admin"
    assert settings.dry_run is True
    assert settings.device_update_policy == "create-only"
    assert settings.site_policy == "require"


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


def test_from_env_parses_port_model_templates_grace_and_skip_report(monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "secret-key")
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PORT_MODEL_TEMPLATES", "USW-Pro-48=Gi1/0/{port};SFP {sfp}")
    monkeypatch.setenv("STALE_OFFLINE_GRACE", "24h")
    monkeypatch.setenv("CABLE_SKIP_REPORT", "/tmp/cable-skips.csv")

    settings = Settings.from_env()

    assert settings.port_model_templates["USWPRO48"] == ["Gi1/0/{port}", "SFP {sfp}"]
    assert settings.stale_offline_grace_seconds == 24 * 3600
    assert settings.cable_skip_report == "/tmp/cable-skips.csv"


def test_from_env_rejects_a_bad_grace_period(monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "secret-key")
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("STALE_OFFLINE_GRACE", "two days")

    with pytest.raises(SystemExit):
        Settings.from_env()


def test_from_env_prefers_real_environment_over_dotenv_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("UNIFI_HOST=https://from-dotenv\n")
    monkeypatch.setenv("UNIFI_HOST", "https://from-real-env")
    monkeypatch.setenv("UNIFI_API_KEY", "secret-key")
    monkeypatch.setenv("NETBOX_URL", REQUIRED_ENV["NETBOX_URL"])
    monkeypatch.setenv("NETBOX_TOKEN", REQUIRED_ENV["NETBOX_TOKEN"])
    monkeypatch.setenv("NETBOX_SITE_SLUG", REQUIRED_ENV["NETBOX_SITE_SLUG"])

    settings = Settings.from_env()

    assert settings.unifi_host == "https://from-real-env"


def test_device_sync_settings_keep_compatible_defaults(monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "secret-key")
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    settings = Settings.from_env()

    assert settings.merge_client_infra is True
    assert settings.unifi_switch_role_slug == "switch"
    assert settings.unifi_ap_role_slug == "wireless-ap"
    assert settings.unifi_gateway_role_slug == "router"
    assert settings.unifi_power_role_slug == "pdu"
    assert settings.inventory_site_ids == ()
    assert settings.inventory_device_status == "inventory"


def test_inventory_sites_accept_api_name_and_internal_id(monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "secret-key")
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("UNIFI_INVENTORY_SITES", "staging, 64aabbccddeeff0011223344")
    monkeypatch.setenv("INVENTORY_DEVICE_STATUS", "staged")
    monkeypatch.setenv("UNIFI_SWITCH_ROLE_SLUG", "access-switch")
    monkeypatch.setenv("MERGE_CLIENT_INFRA", "false")

    settings = Settings.from_env()

    assert settings.inventory_site_ids == ("staging", "64aabbccddeeff0011223344")
    assert settings.inventory_device_status == "staged"
    assert settings.unifi_switch_role_slug == "access-switch"
    assert settings.merge_client_infra is False


def test_inventory_device_status_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "secret-key")
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("INVENTORY_DEVICE_STATUS", "spare")

    with pytest.raises(SystemExit):
        Settings.from_env()


def test_prefetch_and_worker_defaults(monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "secret-key")
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    for key in (
        "NETBOX_PREFETCH",
        "NETBOX_FAST_PATH",
        "NETBOX_PAGE_SIZE",
        "NETBOX_MAX_INFLIGHT",
        "NETBOX_RATE_LIMIT_RETRIES",
        "MAX_WORKERS",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = Settings.from_env()

    assert settings.netbox_prefetch is True
    assert settings.netbox_fast_path is True
    assert settings.netbox_page_size == 1000
    assert settings.netbox_max_inflight == 4
    assert settings.netbox_rate_limit_retries == 5
    assert settings.max_workers == 4


def test_prefetch_settings_can_be_turned_down(monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "secret-key")
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("NETBOX_PREFETCH", "false")
    monkeypatch.setenv("NETBOX_FAST_PATH", "off")
    monkeypatch.setenv("NETBOX_PAGE_SIZE", "50")
    monkeypatch.setenv("NETBOX_MAX_INFLIGHT", "0")
    monkeypatch.setenv("NETBOX_RATE_LIMIT_RETRIES", "2")
    monkeypatch.setenv("MAX_WORKERS", "1")

    settings = Settings.from_env()

    assert settings.netbox_prefetch is False
    assert settings.netbox_fast_path is False
    assert settings.netbox_page_size == 50
    assert settings.netbox_max_inflight == 0
    assert settings.netbox_rate_limit_retries == 2
    assert settings.max_workers == 1


def test_page_size_rejects_zero(monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "secret-key")
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("NETBOX_PAGE_SIZE", "0")

    with pytest.raises(SystemExit):
        Settings.from_env()
