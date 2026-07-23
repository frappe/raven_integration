from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import registry

_FAKE_PATH = "raven_integration.tests.fake_provider.get_provider"


class TestRegistry(FrappeTestCase):
	def _patch_providers(self):
		return patch.object(registry, "_provider_paths", return_value=[_FAKE_PATH])

	def test_list_providers(self):
		with self._patch_providers():
			providers = registry.list_providers()
		names = [p["name"] for p in providers]
		self.assertIn("FAKE", names)

	def test_evaluate_dispatches(self):
		with self._patch_providers():
			result = registry.evaluate("FAKE", "always-ab", {})
		self.assertEqual(result, {"a@example.com", "b@example.com"})

	def test_unknown_provider_raises(self):
		with self._patch_providers():
			with self.assertRaises(frappe.ValidationError):
				registry.evaluate("NOPE", "x", {})

	def test_unknown_rule_type_raises(self):
		with self._patch_providers():
			with self.assertRaises(frappe.ValidationError):
				registry.evaluate("FAKE", "no-such", {})
