from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import registry
from raven_integration.exceptions import ProviderDataError

_FAKE_PATH = "raven_integration.tests.fake_provider.get_provider"
_SLOPPY_PATH = "raven_integration.tests.test_registry.get_sloppy_provider"
_CONDITIONAL_PATH = "raven_integration.tests.test_registry.get_conditional_provider"

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
	# Perfectly ordinary answers a provider gives when it does not go out of its way
	# to build a set: a query generator, and the keys of a dict it grouped by. The
	# generator is behind a callable because a generator is consumed by being read,
	# and a module-level one would be empty for every test after the first.
	"a-generator": lambda: (u for u in ("a@example.com", "b@example.com")),
	"dict-keys": {"a@example.com": 1, "b@example.com": 2}.keys(),
	"unhashable-entries": [["a@example.com"]],
}


def _sloppy_return(rule_type):
	value = _SLOPPY_RETURNS[rule_type]
	return value() if callable(value) else value


def get_sloppy_provider():
	"""A provider registered by another app whose evaluate() is not careful.

	Providers are cross-app code, so the registry is the last place that can notice
	before a wrong answer reaches sync_service and half-applies."""
	return {
		"name": "SLOPPY",
		"label": "Sloppy",
		"rule_types": [{"type": t, "label": t, "fields": []} for t in _SLOPPY_RETURNS],
		"evaluate": lambda rule_type, config: _sloppy_return(rule_type),
	}


def get_conditional_provider():
	"""A provider whose required field only applies to one choice of a Select.

	`depends_on` is what the rule builder renders on, so it has to be what the
	config is judged against too — a field the row never drew cannot be missing.
	"""
	return {
		"name": "CONDITIONAL",
		"label": "Conditional",
		"rule_types": [
			{
				"type": "scoped",
				"label": "Scoped",
				"fields": [
					{
						"fieldname": "mode",
						"fieldtype": "Select",
						"label": "Mode",
						"options": ["role", "tagged"],
						"reqd": 1,
						"default": "role",
					},
					{
						"fieldname": "courses",
						"fieldtype": "MultiSelect",
						"label": "Courses",
						"options": "LMS Course",
						"reqd": 1,
						"depends_on": {"field": "mode", "value": "tagged"},
					},
				],
			}
		],
		"evaluate": lambda rule_type, config: set(),
	}


class TestConditionalFields(FrappeTestCase):
	"""`reqd` is judged against the fields that apply, not every field declared."""

	def _patch(self):
		return patch.object(registry, "_provider_paths", return_value=[_CONDITIONAL_PATH])

	def test_a_field_whose_condition_is_unmet_is_not_required(self):
		with self._patch():
			registry.validate_rule_config("CONDITIONAL", "scoped", {"mode": "role"})

	def test_a_field_whose_condition_is_met_is_required(self):
		with self._patch():
			with self.assertRaises(frappe.ValidationError) as cm:
				registry.validate_rule_config("CONDITIONAL", "scoped", {"mode": "tagged"})
		self.assertIn("Courses", str(cm.exception))

	def test_the_condition_reads_the_declared_default(self):
		"""An unset Select renders its default, so the config is judged as if it
		held it. An empty config is still refused — `mode` is required — but for
		`mode` alone: `courses` depends on a value the default is not, so it is not
		one of the things missing."""
		with self._patch():
			with self.assertRaises(frappe.ValidationError) as cm:
				registry.validate_rule_config("CONDITIONAL", "scoped", {})
		self.assertIn("Mode", str(cm.exception))
		self.assertNotIn("Courses", str(cm.exception))

	def test_a_met_condition_still_passes_once_filled(self):
		with self._patch():
			registry.validate_rule_config("CONDITIONAL", "scoped", {"mode": "tagged", "courses": ["C1"]})


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

	def test_a_generator_of_users_is_accepted(self):
		# The contract is "any iterable", and a provider that yields its users rather
		# than materialising them is not making a mistake. Narrowing this to four
		# concrete collection types stopped such a provider's channels syncing.
		self.assertEqual(self._evaluate("a-generator"), {"a@example.com", "b@example.com"})

	def test_a_dict_keys_view_of_users_is_accepted(self):
		self.assertEqual(self._evaluate("dict-keys"), {"a@example.com", "b@example.com"})

	def test_unhashable_entries_are_reported_as_provider_data(self):
		# set() raises TypeError on these, which the engine's lenient handler does
		# not catch — the same 500 a returned None used to cause.
		with self.assertRaises(ProviderDataError):
			self._evaluate("unhashable-entries")


_BAD_DEP_PATH = "raven_integration.tests.test_registry.get_string_depends_on_provider"
_UNKNOWN_DEP_PATH = "raven_integration.tests.test_registry.get_unknown_depends_on_provider"
_NO_FIELDNAME_PATH = "raven_integration.tests.test_registry.get_unnamed_field_provider"


def _one_rule_type(fields):
	return {
		"name": "BADDECL",
		"label": "Bad declaration",
		"rule_types": [{"type": "scoped", "label": "Scoped", "fields": fields}],
		"evaluate": lambda rule_type, config: set(),
	}


def get_string_depends_on_provider():
	"""A provider that writes `depends_on` the way a Frappe DocField does."""
	return _one_rule_type(
		[
			{"fieldname": "mode", "fieldtype": "Select", "options": ["role"], "default": "role"},
			{"fieldname": "courses", "fieldtype": "MultiSelect", "depends_on": "eval:doc.mode=='role'"},
		]
	)


def get_unknown_depends_on_provider():
	return _one_rule_type(
		[
			{"fieldname": "mode", "fieldtype": "Select", "options": ["role"], "default": "role"},
			# `mdoe`, not `mode`.
			{
				"fieldname": "courses",
				"fieldtype": "MultiSelect",
				"depends_on": {"field": "mdoe", "value": "role"},
			},
		]
	)


def get_unnamed_field_provider():
	return _one_rule_type([{"fieldtype": "Select", "label": "Mode", "options": ["role"]}])


class TestDeclarationValidation(FrappeTestCase):
	"""A rule-type declaration is a schema two sides dereference, so it is checked once.

	`validate_rule_config` runs on every channel save. A declaration it cannot read
	used to surface there as an AttributeError out of the save path — a 500 on the
	settings page, naming neither the hook nor the app that produced it.
	"""

	def _evaluate_with(self, path):
		with patch.object(registry, "_provider_paths", return_value=[path]):
			registry.validate_rule_config("BADDECL", "scoped", {"mode": "role"})

	def test_frappes_string_depends_on_is_refused_by_name(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._evaluate_with(_BAD_DEP_PATH)
		message = str(cm.exception)
		self.assertIn("depends_on", message)
		self.assertIn("courses", message)

	def test_depends_on_naming_a_field_the_rule_type_does_not_declare_is_refused(self):
		# Silently satisfied by nothing, so the field is hidden on every rule of this
		# type forever and no row can ever be completed.
		with self.assertRaises(frappe.ValidationError) as cm:
			self._evaluate_with(_UNKNOWN_DEP_PATH)
		self.assertIn("mdoe", str(cm.exception))

	def test_a_field_with_no_fieldname_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._evaluate_with(_NO_FIELDNAME_PATH)
		self.assertIn("fieldname", str(cm.exception))

	def test_a_sound_conditional_declaration_still_loads(self):
		# The guard above must not be reachable by the shape the LMS provider uses.
		with patch.object(registry, "_provider_paths", return_value=[_CONDITIONAL_PATH]):
			registry.validate_rule_config("CONDITIONAL", "scoped", {"mode": "role"})
