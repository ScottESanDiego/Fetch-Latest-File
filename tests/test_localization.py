"""Check that UI metadata and translated text stay complete and separate."""

import ast
import json
import unittest
from pathlib import Path

import yaml

from custom_components import fetch_latest_file as integration
from custom_components.fetch_latest_file import const

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "fetch_latest_file"


class LocalizationTests(unittest.TestCase):
    def setUp(self):
        self.translations = json.loads((ROOT / "translations" / "en.json").read_text())
        self.services = yaml.safe_load((ROOT / "services.yaml").read_text())

    def test_all_service_inputs_have_translated_labels_and_ui_selectors(self):
        fields = self.services["fetch"]["fields"]
        schema_keys = {key.schema for key in integration.SERVICE_FETCH_SCHEMA.schema}
        self.assertEqual(set(fields), schema_keys)
        translated = self.translations["services"]["fetch"]
        self.assertEqual(set(translated["fields"]), schema_keys)
        for key, field in fields.items():
            with self.subTest(field=key):
                self.assertTrue(field["selector"])
                self.assertTrue(translated["fields"][key]["name"])
                self.assertTrue(translated["fields"][key]["description"])
                self.assertNotIn("name", field)
                self.assertNotIn("description", field)
                self.assertNotIn("example", translated["fields"][key])
        self.assertTrue(translated["name"])
        self.assertTrue(translated["description"])
        self.assertNotIn("name", self.services["fetch"])
        self.assertNotIn("description", self.services["fetch"])

    def test_options_have_labels_and_help_for_every_schema_field(self):
        # Inspect the real options schema without loading config-flow HA dependencies.
        tree = ast.parse((ROOT / "config_flow.py").read_text())
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_options_schema"
        )
        keys = {
            getattr(const, node.args[0].id)
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Optional"
            and isinstance(node.args[0], ast.Name)
        }
        options = self.translations["options"]["step"]["init"]
        self.assertEqual(set(options["data"]), keys)
        self.assertEqual(set(options["data_description"]), keys)
        self.assertTrue(all(options["data"].values()))
        self.assertTrue(all(options["data_description"].values()))

    def test_custom_integration_uses_runtime_translations(self):
        self.assertFalse((ROOT / "strings.json").exists())
        self.assertNotIn("[%key:", (ROOT / "translations" / "en.json").read_text())
        self.assertTrue(self.translations["config"]["abort"]["already_configured"])
        self.assertNotIn(
            "single_instance_allowed", self.translations["config"]["abort"]
        )
