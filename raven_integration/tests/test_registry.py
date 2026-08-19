from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import registry
from raven_integration.exceptions import ProviderDataError

_FAKE_PATH = "raven_integration.tests.fake_provider.get_provider"
_SLOPPY_PATH = "raven_integration.tests.test_registry.get_sloppy_provider"

# What a third-party evaluate() might hand back instead of a population. Every one
# of these is something set() accepts silently or blows up on somewhere further down.
_SLOPPY_RETURNS = {
	"a-bare-string": "a@example.com",
	"nothing": None,
	"a-list": ["a@example.com", "b@example.com"],
	"a-tuple": ("a@example.com",),
	"a-frozenset": frozenset({"a@example.com"}),
	"a-number-among-the-users": ["a@example.com", 7],
	"a-blank-among-the-users": ["a@example.com", "   "],
}


def get_sloppy_provider():
	"""A provider registered by another app whose evaluate() is not careful.

	Providers are cross-app code, so the registry is the last place that can notice
	before a wrong answer reaches sync_service and half-applies."""
	return {
		"name": "SLOPPY",
		"label": "Sloppy",
		"rule_types": [{"type": t, "label": t, "fields": []} for t in _SLOPPY_RETURNS],
		"evaluate": lambda rule_type, config: _SLOPPY_RETURNS[rule_type],
	}


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


class TestProviderReturnValue(FrappeTestCase):
	def _patch_providers(self):
		return patch.object(registry, "_provider_paths", return_value=[_SLOPPY_PATH])

	def _evaluate(self, rule_type):
		with self._patch_providers():
			return registry.evaluate("SLOPPY", rule_type, {})

	def test_a_returned_string_is_not_a_population_of_single_characters(self):
		# set("a@example.com") is seven single-character "users", and sync_service adds
		# them one at a time until one fails — after earlier adds have already landed.
		with self.assertRaises(ProviderDataError):
			self._evaluate("a-bare-string")

	def test_a_provider_that_returns_nothing_is_reported_as_provider_data(self):
		# set(None) is a TypeError, which the engine's lenient handler does not catch,
		# so the channel detail page 500s on a request that only wanted to display.
		with self.assertRaises(ProviderDataError):
			self._evaluate("nothing")

	def test_a_non_string_among_the_users_is_rejected(self):
		with self.assertRaises(ProviderDataError):
			self._evaluate("a-number-among-the-users")

	def test_a_blank_user_id_is_rejected(self):
		with self.assertRaises(ProviderDataError):
			self._evaluate("a-blank-among-the-users")

	def test_the_error_names_the_provider_and_the_rule_type(self):
		# Whoever reads this in an Error Log has to know which app to go and fix.
		with self.assertRaises(ProviderDataError) as caught:
			self._evaluate("a-bare-string")
		self.assertIn("SLOPPY", str(caught.exception))
		self.assertIn("a-bare-string", str(caught.exception))

	def test_a_list_of_users_is_accepted(self):
		self.assertEqual(self._evaluate("a-list"), {"a@example.com", "b@example.com"})

	def test_a_tuple_of_users_is_accepted(self):
		self.assertEqual(self._evaluate("a-tuple"), {"a@example.com"})

	def test_a_frozenset_of_users_is_accepted(self):
		self.assertEqual(self._evaluate("a-frozenset"), {"a@example.com"})
