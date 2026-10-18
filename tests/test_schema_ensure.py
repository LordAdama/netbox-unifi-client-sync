"""Schema ensures stay successful when another worker creates the object first.

Documentation host only: netbox.example.com. No site data.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pynetbox
import pytest

from unifi_netbox_sync.caching import CachingNetboxGateway
from unifi_netbox_sync.netbox_client import PynetboxGateway
from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings


def _request_error(body: str, status: int = 400) -> pynetbox.RequestError:
    response = MagicMock()
    response.status_code = status
    response.reason = "Bad Request"
    response.url = "https://netbox.example.com/api/extras/custom-fields/"
    response.request = MagicMock(body=b"")
    response.text = body
    response.json.return_value = {"name": [body]}
    return pynetbox.RequestError(response)


def _gateway(api) -> PynetboxGateway:
    gateway = PynetboxGateway.__new__(PynetboxGateway)
    gateway.api = api
    return gateway


class _Field:
    def __init__(self, name: str, object_types: list[str]) -> None:
        self.name = name
        self.object_types = list(object_types)
        self.saved = 0

    def save(self) -> None:
        self.saved += 1


def test_custom_field_already_exists_is_refetched():
    """The loser of a create race loads the field the winner stored."""
    field = _Field("unifi_macs", ["dcim.device"])
    endpoint = MagicMock()
    endpoint.get.side_effect = [None, field]
    endpoint.create.side_effect = _request_error("custom field with this Name already exists.")
    gateway = _gateway(SimpleNamespace(extras=SimpleNamespace(custom_fields=endpoint)))

    gateway._ensure_custom_field("unifi_macs", "UniFi MACs", ["dcim.device"])

    endpoint.create.assert_called_once()
    assert endpoint.get.call_count == 2
    assert field.saved == 0


def test_custom_field_unique_violation_is_refetched():
    field = _Field("unifi_macs", ["dcim.device"])
    endpoint = MagicMock()
    endpoint.get.side_effect = [None, field]
    endpoint.create.side_effect = _request_error(
        'duplicate key value violates unique constraint "extras_customfield_name_key"'
    )
    gateway = _gateway(SimpleNamespace(extras=SimpleNamespace(custom_fields=endpoint)))

    gateway._ensure_custom_field("unifi_macs", "UniFi MACs", ["dcim.device"])

    assert endpoint.get.call_count == 2


def test_custom_field_other_errors_still_raise():
    endpoint = MagicMock()
    endpoint.get.return_value = None
    endpoint.create.side_effect = _request_error("name: this field is required")
    gateway = _gateway(SimpleNamespace(extras=SimpleNamespace(custom_fields=endpoint)))

    with pytest.raises(pynetbox.RequestError):
        gateway._ensure_custom_field("unifi_macs", "UniFi MACs", ["dcim.device"])


def test_custom_field_conflict_without_a_row_still_raises():
    endpoint = MagicMock()
    endpoint.get.return_value = None
    endpoint.create.side_effect = _request_error("custom field with this Name already exists.")
    gateway = _gateway(SimpleNamespace(extras=SimpleNamespace(custom_fields=endpoint)))

    with pytest.raises(pynetbox.RequestError):
        gateway._ensure_custom_field("unifi_macs", "UniFi MACs", ["dcim.device"])


def test_two_threads_creating_the_same_custom_field_both_succeed():
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    state: dict[str, object] = {"field": None, "creates": 0, "gets": 0}

    class Fields:
        def get(self, name: str):
            with lock:
                state["gets"] = int(state["gets"]) + 1
                n = int(state["gets"])
            if n <= 2:
                barrier.wait()
                return None
            return state["field"]

        def create(self, **kwargs):
            with lock:
                state["creates"] = int(state["creates"]) + 1
                if state["field"] is not None:
                    raise _request_error("custom field with this Name already exists.")
                state["field"] = _Field(kwargs["name"], list(kwargs["object_types"]))
                return state["field"]

    gateway = _gateway(SimpleNamespace(extras=SimpleNamespace(custom_fields=Fields())))
    errors: list[BaseException] = []

    def run() -> None:
        try:
            gateway._ensure_custom_field("unifi_macs", "UniFi MACs", ["dcim.device"])
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert state["creates"] == 2
    assert isinstance(state["field"], _Field)


class _ConflictEndpoint:
    """get-then-create where every create loses to a row that is already stored."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.objects: dict[str, SimpleNamespace] = {}
        self.creates = 0

    def get(self, **kwargs):
        key = kwargs.get("slug") or kwargs.get("name")
        return self.objects.get(key)

    def create(self, **kwargs):
        key = kwargs.get("slug") or kwargs.get("name")
        self.creates += 1
        self.objects[key] = SimpleNamespace(
            id=self.creates,
            slug=kwargs.get("slug"),
            name=kwargs.get("name"),
            object_types=list(kwargs.get("object_types") or []),
            save=lambda: None,
        )
        raise _request_error(f"{self.kind} with this name already exists.")


def test_ensure_prerequisites_treats_already_exists_as_success():
    api = SimpleNamespace(
        extras=SimpleNamespace(
            tags=_ConflictEndpoint("tag"),
            custom_fields=_ConflictEndpoint("custom field"),
        ),
        dcim=SimpleNamespace(
            manufacturers=_ConflictEndpoint("manufacturer"),
            device_types=_ConflictEndpoint("device type"),
            device_roles=_ConflictEndpoint("device role"),
        ),
    )
    gateway = _gateway(api)

    gateway.ensure_prerequisites("unifi-client", "generic", "generic-network-client", "unifi-sync")

    assert "unifi_macs" in api.extras.custom_fields.objects
    assert "unifi_mac" in api.extras.custom_fields.objects
    assert "unifi-sync" in api.extras.tags.objects
    assert "generic" in api.dcim.manufacturers.objects
    assert "generic-network-client" in api.dcim.device_types.objects
    assert "unifi-client" in api.dcim.device_roles.objects
    creates = (
        api.extras.custom_fields.creates
        + api.extras.tags.creates
        + api.dcim.manufacturers.creates
        + api.dcim.device_types.creates
        + api.dcim.device_roles.creates
    )
    gateway.ensure_prerequisites("unifi-client", "generic", "generic-network-client", "unifi-sync")
    assert (
        api.extras.custom_fields.creates
        + api.extras.tags.creates
        + api.dcim.manufacturers.creates
        + api.dcim.device_types.creates
        + api.dcim.device_roles.creates
    ) == creates


def test_schema_is_ensured_on_the_main_thread_before_site_workers():
    netbox = FakeNetboxGateway()
    callers: list[str] = []
    original = netbox.ensure_prerequisites

    def wrapped(*args, **kwargs):
        callers.append(threading.current_thread().name)
        return original(*args, **kwargs)

    netbox.ensure_prerequisites = wrapped
    by_site = {
        "hq": [make_unifi_client(mac="00:00:5e:00:53:01", name="client-a", is_wired=False)],
        "branch": [make_unifi_client(mac="00:00:5e:00:53:02", name="client-b", is_wired=False)],
    }
    summary = SyncEngine(
        unifi=FakeUnifiClient(by_site=by_site),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(
            site_map={"hq": "headquarters", "branch": "branch-office"},
            max_workers=2,
        ),
    ).run()

    assert summary.errors == []
    assert summary.devices_created == 2
    assert callers == ["MainThread"]
