"""Small Home Assistant boundary doubles; integration logic is imported unchanged."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from enum import Enum
import sys
import unittest
from types import ModuleType, SimpleNamespace

import voluptuous as vol


def install_home_assistant_stubs():
    def module(name, **attributes):
        result = ModuleType(name)
        result.__dict__.update(attributes)
        sys.modules[name] = result
        return result

    class ConfigEntry:
        @classmethod
        def __class_getitem__(cls, item):
            return cls

        def __init__(self):
            self.entry_id = "test-entry"
            self.options = {}
            self.runtime_data = None

    class Platform(Enum):
        SENSOR = "sensor"

    class SensorEntity:
        entity_id = "sensor.fetch_latest_file"
        writes = 0

        def async_write_ha_state(self):
            self.writes += 1

    def string(value):
        if not isinstance(value, str):
            raise vol.Invalid("expected string")
        return value

    def boolean(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise vol.Invalid("expected boolean")

    module("homeassistant")
    module("homeassistant.config_entries", ConfigEntry=ConfigEntry)
    module("homeassistant.const", Platform=Platform)
    module("homeassistant.core", HomeAssistant=object, ServiceCall=SimpleNamespace)
    module("homeassistant.helpers")
    module("homeassistant.helpers.config_validation", string=string, boolean=boolean)
    module("homeassistant.util")
    module("homeassistant.util.dt", now=lambda: datetime.now(timezone.utc))
    module("homeassistant.components")
    module("homeassistant.components.sensor", SensorEntity=SensorEntity,
           SensorDeviceClass=SimpleNamespace(TIMESTAMP="timestamp"))
    module("homeassistant.helpers.device_registry", DeviceEntryType=SimpleNamespace(SERVICE="service"))
    module("homeassistant.helpers.entity", DeviceInfo=dict)
    module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    return ConfigEntry


ConfigEntry = install_home_assistant_stubs()


class AsyncCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Some restricted hosts do not wake selector loops for cross-thread
        # self-pipe writes. A periodic timer lets genuine executor/watchdog
        # callbacks drain without replacing their threading behavior.
        self._pulse_handle = None
        def pulse():
            self._pulse_handle = asyncio.get_running_loop().call_later(.01, pulse)
        pulse()
        self.addCleanup(lambda: self._pulse_handle.cancel())


class Services:
    def __init__(self):
        self.registered = {}

    def has_service(self, domain, name):
        return (domain, name) in self.registered

    def async_register(self, domain, name, handler, schema):
        self.registered[domain, name] = (handler, schema)

    def async_remove(self, domain, name):
        self.registered.pop((domain, name))

    async def call(self, data):
        handler, schema = self.registered["fetch_latest_file", "fetch"]
        await handler(SimpleNamespace(data=schema(data)))


class Hass:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=8)
        self.services = Services()
        self.config_entries = SimpleNamespace(
            async_forward_entry_setups=self.forward,
            async_unload_platforms=self.unload,
            async_loaded_entries=lambda domain: [],
        )

    async def async_add_executor_job(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self.executor, function, *args)

    async def forward(self, entry, platforms):
        from custom_components.fetch_latest_file.sensor import FetchLatestFileSensor
        entry.runtime_data.sensor_entity = FetchLatestFileSensor(entry, 20, 24)

    async def unload(self, entry, platforms):
        return True
