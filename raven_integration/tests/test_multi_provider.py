"""Two providers registered at once — the case the rest of the suite never exercises."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import engine, registry

_FAKE_PATH = "raven_integration.tests.fake_provider.get_provider"
_FAKE2_PATH = "raven_integration.tests.fake_provider_two.get_provider"
_COLLIDING_PATH = "raven_integration.tests.test_multi_provider.get_colliding_provider"
_BROKEN_PATH = "raven_integration.tests.test_multi_provider.get_provider_without_evaluate"


def get_colliding_provider() -> dict:
	"""A third app declaring FAKE's name — the collision G2 must refuse."""
	return {
		"name": "FAKE",
		"label": "Impostor",
		"rule_types": [{"type": "always-z", "label": "Always Z", "fields": []}],
		"evaluate": lambda rule_type, config: {"z@example.com"},
		"triggers": [],
	}


def get_provider_without_evaluate() -> dict:
	"""A declaration the registry cannot dispatch to."""
	return {
		"name": "NOEVAL",
		"label": "No Evaluate",
		"rule_types": [{"type": "whatever", "label": "Whatever", "fields": []}],
	}


def _rule(provider: str, rule_type: str, status: str = "Active") -> dict:
	return {
		"label": f"{provider} {rule_type}",
		"provider": provider,
		"rule_type": rule_type,
		"status": status,
		"config": {},
	}


def _both_providers():
	return patch.object(registry, "_provider_paths", return_value=[_FAKE_PATH, _FAKE2_PATH])


class TestTwoProvidersCoexist(FrappeTestCase):
	def test_both_providers_are_listed(self):
		with _both_providers():
			names = [p["name"] for p in registry.list_providers()]
		self.assertEqual(sorted(names), ["FAKE", "FAKE2"])

	def test_trigger_doctypes_unions_across_providers(self):
		"""Each app's triggers must survive; the shared one is not double-counted."""
		with _both_providers():
			triggers = registry.trigger_doctypes()
		self.assertEqual(triggers, {"Some Doctype", "Another Doctype", "Third Doctype"})

	def test_each_rule_is_evaluated_by_its_own_provider(self):
		with _both_providers():
			fake = registry.evaluate("FAKE", "always-ab", {})
			fake2 = registry.evaluate("FAKE2", "b-and-c", {})
		self.assertEqual(fake, {"a@example.com", "b@example.com"})
		self.assertEqual(fake2, {"b@example.com", "c@example.com"})

	def test_a_rule_type_of_the_other_provider_is_rejected(self):
		"""Rule types are per provider — FAKE2's type must not resolve against FAKE."""
		with _both_providers():
			with self.assertRaises(frappe.ValidationError):
				registry.evaluate("FAKE", "b-and-c", {})

	def test_list_rule_types_is_scoped_to_the_provider(self):
		with _both_providers():
			fake = [rt["type"] for rt in registry.list_rule_types("FAKE")]
			fake2 = [rt["type"] for rt in registry.list_rule_types("FAKE2")]
		self.assertEqual(sorted(fake), ["always-a", "always-ab"])
		self.assertEqual(sorted(fake2), ["b-and-c", "only-b"])

	def test_list_rule_types_rejects_an_unknown_provider(self):
		with _both_providers():
			with self.assertRaises(frappe.ValidationError) as cm:
				registry.list_rule_types("NOPE")
		self.assertIn("NOPE", str(cm.exception))


class TestMixedProviderMapping(FrappeTestCase):
	"""One rule list holding rules from two different providers."""

	def setUp(self):
		self.rules = [_rule("FAKE", "always-ab"), _rule("FAKE2", "b-and-c")]

	def test_any_or_unions_both_providers(self):
		with _both_providers():
			members = engine.evaluate_rules(self.rules, combinator="Any (OR)")
		self.assertEqual(members, {"a@example.com", "b@example.com", "c@example.com"})

	def test_all_and_intersects_both_providers(self):
		with _both_providers():
			members = engine.evaluate_rules(self.rules, combinator="All (AND)")
		self.assertEqual(members, {"b@example.com"})

	def test_a_paused_rule_of_either_provider_drops_out(self):
		rules = [_rule("FAKE", "always-ab"), _rule("FAKE2", "b-and-c", status="Paused")]
		with _both_providers():
			members = engine.evaluate_rules(rules, combinator="Any (OR)")
		self.assertEqual(members, {"a@example.com", "b@example.com"})


class TestProviderDeclarationIsValidated(FrappeTestCase):
	def test_duplicate_provider_name_is_refused(self):
		with patch.object(
			registry, "_provider_paths", return_value=[_FAKE_PATH, _COLLIDING_PATH]
		):
			with self.assertRaises(frappe.ValidationError) as cm:
				registry.list_providers()
		message = str(cm.exception)
		# The message must name the provider and BOTH hook paths, or the user cannot
		# tell which two apps clash.
		self.assertIn("FAKE", message)
		self.assertIn(_FAKE_PATH, message)
		self.assertIn(_COLLIDING_PATH, message)

	def test_declaration_missing_evaluate_is_refused(self):
		with patch.object(registry, "_provider_paths", return_value=[_BROKEN_PATH]):
			with self.assertRaises(frappe.ValidationError) as cm:
				registry.list_providers()
		message = str(cm.exception)
		self.assertIn("evaluate", message)
		self.assertIn(_BROKEN_PATH, message)

	def test_a_valid_pair_still_loads(self):
		"""The validation must not reject the declarations the fixtures actually use."""
		with _both_providers():
			self.assertEqual(len(registry.list_providers()), 2)


class TestApiPathsForANonLmsProvider(FrappeTestCase):
	"""preview_rule / compute_rule_diff are what a second app's UI would call."""

	def setUp(self):
		with _both_providers():
			ws = frappe.new_doc("Raven Workspace Mapping")
			ws.workspace_label = f"Multi Provider WS {frappe.generate_hash(length=6)}"
			ws.workspace_type = "Private"
			ws.rule_combinator = "Any (OR)"
			ws.flags.skip_raven_create = True
			ws.append("member_rules", _rule("FAKE", "always-ab"))
			ws.append("member_rules", _rule("FAKE2", "b-and-c"))
			ws.insert()
		self.workspace = ws.name
		self.addCleanup(
			lambda: frappe.delete_doc(
				"Raven Workspace Mapping", self.workspace, force=True, ignore_missing=True
			)
		)

	def test_a_mapping_can_store_rules_from_both_providers(self):
		rules = frappe.get_doc("Raven Workspace Mapping", self.workspace).member_rules
		self.assertEqual(sorted(r.provider for r in rules), ["FAKE", "FAKE2"])

	def test_preview_rule_works_for_the_second_provider(self):
		from raven_integration.api import preview_rule

		with _both_providers():
			result = preview_rule({"provider": "FAKE2", "rule_type": "b-and-c", "config": {}})
		self.assertEqual(result["matched_user_count"], 2)
		self.assertEqual(result["sample_users"], ["b@example.com", "c@example.com"])

	def test_preview_rule_rejects_a_rule_type_of_the_other_provider(self):
		from raven_integration.api import preview_rule

		with _both_providers():
			with self.assertRaises(frappe.ValidationError):
				preview_rule({"provider": "FAKE2", "rule_type": "always-ab", "config": {}})

	def test_compute_rule_diff_adds_the_second_provider_population(self):
		"""Nobody is synced yet, so every matched user of both providers is an add."""
		from raven_integration.api import compute_rule_diff

		with _both_providers():
			result = compute_rule_diff(
				target_doctype="Raven Workspace Mapping",
				name=self.workspace,
				new_rules=[_rule("FAKE2", "b-and-c")],
			)
		self.assertEqual(result["added"], 2)
		self.assertEqual(result["removed"], 0)

	def test_compute_rule_diff_honours_the_combinator_across_providers(self):
		"""Saved rules span both providers: OR adds three users, AND only the shared one."""
		from raven_integration.api import compute_rule_diff

		with _both_providers():
			any_or = compute_rule_diff(
				target_doctype="Raven Workspace Mapping",
				name=self.workspace,
				combinator="Any (OR)",
			)
			all_and = compute_rule_diff(
				target_doctype="Raven Workspace Mapping",
				name=self.workspace,
				combinator="All (AND)",
			)
		self.assertEqual(any_or["added"], 3)
		self.assertEqual(all_and["added"], 1)
