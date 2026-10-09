"""Exercise real service registration, validation, sensor writes and unload."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import voluptuous as vol

from support import AsyncCase, ConfigEntry, Hass
from custom_components import fetch_latest_file as integration
from custom_components.fetch_latest_file.scanner import MatchedFile


class SchemaTests(unittest.TestCase):
    def test_schema_defaults_and_current_min_size_spelling(self):
        data = integration.SERVICE_FETCH_SCHEMA({"directory": "/tmp", "filename": "upload"})
        self.assertEqual(data["min_size"], "0B")
        self.assertIs(data["wait_for_upload"], False)
        self.assertEqual(data["timeout"], 30)
        self.assertEqual(data["quiet_period"], 2)
        for old_name in ("MinSize", "minsize"):
            with self.subTest(name=old_name), self.assertRaises(vol.Invalid):
                integration.SERVICE_FETCH_SCHEMA({"directory": "/tmp", "filename": "", old_name: "1M"})

    def test_wait_durations_must_be_finite_and_positive(self):
        for field in ("timeout", "quiet_period"):
            for value in (0, -1, "nan", float("nan"), "inf", float("inf"), -float("inf")):
                with self.subTest(field=field, value=value), self.assertRaises(vol.Invalid):
                    integration.SERVICE_FETCH_SCHEMA({"directory": "/tmp", "filename": "", field: value})


class ServiceTests(AsyncCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.hass = Hass()
        self.addCleanup(self.hass.executor.shutdown)
        self.entry = ConfigEntry()
        await integration.async_setup_entry(self.hass, self.entry)
        self.sensor = self.entry.runtime_data.sensor_entity
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = self.temp.name

    def data(self, **kwargs):
        data = {"directory": self.directory, "filename": "upload"}
        data.update(kwargs)
        return data

    async def test_atomic_namespaced_results_and_diagnostics_reset(self):
        path = Path(self.directory) / "upload.jpg"
        path.write_bytes(b"complete")
        with self.assertLogs(integration.__name__, "ERROR"):
            await self.hass.services.call(self.data(min_size="broken"))
        bad = self.sensor._attr_extra_state_attributes["default"]
        self.assertEqual(bad["FetchStatus"]["status"], "error")
        self.assertNotIn("Overall", bad)
        await self.hass.services.call(self.data())
        result = self.sensor._attr_extra_state_attributes["default"]
        self.assertEqual(result["Overall"], str(path))
        self.assertEqual(result["jpg"], str(path))
        self.assertEqual(result["FetchStatus"], {"status": "ok", "reasons": {}})
        self.assertNotIn("error", result)
        self.assertEqual(self.sensor.writes, 2)
        self.assertEqual(self.sensor._attr_native_value, result["timestamp"])

    async def test_search_validation_replaces_old_paths_with_error(self):
        path = Path(self.directory) / "upload.jpg"
        path.write_bytes(b"complete")
        await self.hass.services.call(self.data())
        with self.assertLogs(integration.__name__, "ERROR"):
            await self.hass.services.call(self.data(filename="../upload"))
        result = self.sensor._attr_extra_state_attributes["default"]
        self.assertEqual(result["FetchStatus"]["status"], "error")
        self.assertNotIn("Overall", result)
        self.assertEqual(self.entry.runtime_data.active_fetches, set())

    async def test_no_match_publishes_without_raising_and_warning_is_visible(self):
        with self.assertLogs(integration.__name__, "WARNING") as logs:
            await self.hass.services.call(self.data(wait_for_upload=True, timeout=.02, quiet_period=10))
        self.assertTrue(any("Degraded fetch result" in message for message in logs.output))
        result = self.sensor._attr_extra_state_attributes["default"]
        self.assertEqual(result["status"], "No matching files")
        self.assertEqual(result["FetchStatus"]["status"], "no_match")

    async def test_same_target_serialized_different_targets_run_concurrently(self):
        entered = []
        release = asyncio.Event()
        async def fetch(hass, criteria, **kwargs):
            entered.append(criteria.filename_prefix)
            await release.wait()
            return [MatchedFile(1, "/upload.jpg", "jpg")], {"status": "ok", "reasons": {}}
        with patch.object(integration, "async_fetch_files", fetch):
            first = asyncio.create_task(self.hass.services.call(self.data(filename="first", target_id="same")))
            second = asyncio.create_task(self.hass.services.call(self.data(filename="second", target_id="same")))
            different = asyncio.create_task(self.hass.services.call(self.data(filename="different", target_id="other")))
            await asyncio.sleep(.03)
            self.assertEqual(entered, ["first", "different"])
            release.set()
            await asyncio.gather(first, second, different)
        self.assertEqual(entered, ["first", "different", "second"])
        self.assertEqual(set(self.sensor._attr_extra_state_attributes), {"same", "other"})

    async def test_unload_cancels_active_and_queued_calls_and_awaits_cleanup(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def fetch(hass, criteria, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(.02)
                cleaned.set()
        with patch.object(integration, "async_fetch_files", fetch):
            first = asyncio.create_task(self.hass.services.call(self.data()))
            await entered.wait()
            queued = asyncio.create_task(self.hass.services.call(self.data()))
            await asyncio.sleep(.02)
            self.assertEqual(len(self.entry.runtime_data.active_fetches), 2)
            self.assertTrue(await integration.async_unload_entry(self.hass, self.entry))
            self.assertTrue(cleaned.is_set())
            for task in (first, queued):
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(self.entry.runtime_data.active_fetches, set())
        self.assertIsNone(self.entry.runtime_data.sensor_entity)
        self.assertFalse(self.hass.services.has_service("fetch_latest_file", "fetch"))

    async def test_failed_unload_restores_service_availability(self):
        async def fail(entry, platforms):
            return False
        self.hass.config_entries.async_unload_platforms = fail
        with self.assertLogs(integration.__name__, "ERROR"):
            self.assertFalse(await integration.async_unload_entry(self.hass, self.entry))
        self.assertFalse(self.entry.runtime_data.unloading)
        self.assertIs(self.entry.runtime_data.sensor_entity, self.sensor)
        self.assertTrue(self.hass.services.has_service("fetch_latest_file", "fetch"))
