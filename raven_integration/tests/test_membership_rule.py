"""Member-rule validation as production reaches it: through a parent mapping save."""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import events, registry

_FAKE = ["raven_integration.tests.fake_provider.get_provider"]
_REQCFG = ["raven_integration.tests.test_membership_rule.get_provider_with_required_field"]


def get_provider_with_required_field() -> dict:
	"""A provider whose rule type declares a reqd config field."""
	return {
		"name": "REQCFG",
		"label": "Requires Config",
		"rule_types": [
			{
				"type": "needs-batch",
				"label": "Needs Batch",
				"fields": [{"fieldname": "batch", "label": "Batch", "reqd": 1}],
			}
		],
		"evaluate": lambda rule_type, config: {config["batch"]},
		"triggers": [],
	}


class TestMemberRuleValidation(FrappeTestCase):
	def _insert_under(self, doc, provider_paths: list[str]) -> None:
		# insert() fires the wildcard after_insert -> on_provider_doc_change ->
		# _trigger_doctypes(), which under the fake registry caches an empty trigger
		# set in redis that outlives the DB rollback. Clear it so later tests
		# recompute from the real registry.
		self.addCleanup(events.clear_trigger_doctypes_cache)
		with patch.object(registry, "_provider_paths", lambda: provider_paths):
			doc.insert()

	def _new_workspace(self):
		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = f"Rule Validation WS {frappe.generate_hash(length=6)}"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		return ws

	def _new_channel(self):
		"""A channel mapping under a fresh workspace, not yet inserted."""
		ws = self._new_workspace()
		self._insert_under(ws, _FAKE)
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", ws.name, force=True, ignore_missing=True)
		)
		ch = frappe.new_doc("Raven Channel Mapping")
		ch.channel_label = f"Rule Validation CH {frappe.generate_hash(length=6)}"
		ch.channel_type = "Private"
		ch.workspace = ws.name
		ch.flags.skip_raven_create = True
		return ch

	def _save_mapping_with_rule(self, provider_paths: list[str], **rule):
		"""Insert a channel mapping carrying one member rule; returns the inserted doc.

		A channel is the only mapping that carries rules, so this is also what the
		channel-specific cases below exercise. The rule goes in as a one-leaf tree —
		the shape the JSON column holds now that there is no child table to append to."""
		ch = self._new_channel()
		ch.member_rules_json = frappe.as_json({"conjunctions": [], "conditions": [rule]})
		self._insert_under(ch, provider_paths)
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", ch.name, force=True, ignore_missing=True)
		)
		return ch

	_save_channel_with_rule = _save_mapping_with_rule

	def _leaf(self, ch, index: int = 0) -> dict:
		"""One leaf of the mapping's stored tree."""
		return frappe.parse_json(ch.member_rules_json)["conditions"][index]

	def test_a_sibling_duplicate_is_kept(self):
		"""Two identical siblings save. The check that refused them only ever saw
		leaves that were literally equal, so it caught the one restatement a reader
		can already see and missed every other: two groups evaluating to the same
		population, or a leaf the group above it already subsumes. A rule that says
		nothing new is harmless — it adds the people it names, who are added
		already — so half a check bought nothing but a save the user could not make."""
		ch = self._new_channel()
		rule = {"label": "x", "provider": "FAKE", "rule_type": "always-a", "status": "Active", "config": {}}
		ch.member_rules_json = frappe.as_json({"conjunctions": ["or"], "conditions": [rule, dict(rule)]})
		self._insert_under(ch, _FAKE)
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", ch.name, force=True, ignore_missing=True)
		)
		self.assertEqual(len(frappe.parse_json(ch.member_rules_json)["conditions"]), 2)

	def test_the_same_rule_in_two_groups_is_allowed(self):
		# `A and (A or B)` narrows rather than repeats, and the flat model this
		# replaced could not say it at all.
		ch = self._new_channel()
		rule = {"label": "x", "provider": "FAKE", "rule_type": "always-a", "status": "Active", "config": {}}
		ch.member_rules_json = frappe.as_json(
			{
				"conjunctions": ["and"],
				"conditions": [rule, {"conjunctions": [], "conditions": [dict(rule)]}],
			}
		)
		self._insert_under(ch, _FAKE)
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", ch.name, force=True, ignore_missing=True)
		)
		self.assertEqual(len(frappe.parse_json(ch.member_rules_json)["conditions"]), 2)

	def test_a_paused_rule_under_and_is_rejected(self):
		# Pausing freezes a contribution; under `and` the rule narrows instead, so
		# dropping it would add people. The invariant is enforced on every save path.
		ch = self._new_channel()
		ch.member_rules_json = frappe.as_json(
			{
				"conjunctions": ["and"],
				"conditions": [
					{
						"label": "a",
						"provider": "FAKE",
						"rule_type": "always-a",
						"status": "Active",
						"config": {},
					},
					{
						"label": "b",
						"provider": "FAKE",
						"rule_type": "always-ab",
						"status": "Paused",
						"config": {},
					},
				],
			}
		)
		with self.assertRaises(frappe.ValidationError) as cm:
			self._insert_under(ch, _FAKE)
		self.assertIn("pausing", str(cm.exception).lower())

	def test_stores_provider_type_and_opaque_config(self):
		ch = self._save_mapping_with_rule(
			_FAKE,
			label="My Rule",
			provider="FAKE",
			rule_type="always-ab",
			status="Active",
			config=json.dumps({"courses": ["C1"]}),
		)
		rule = self._leaf(ch)
		self.assertEqual(rule["provider"], "FAKE")
		self.assertEqual(json.loads(rule["config"]), {"courses": ["C1"]})

	def test_a_rule_with_no_provider_is_rejected(self):
		"""There is no child doctype left to declare provider/rule_type mandatory, and
		the registry returns early on an empty one — so the engine has to refuse it."""
		with self.assertRaises(frappe.ValidationError) as cm:
			self._save_mapping_with_rule(_FAKE, label="x", status="Active")
		self.assertIn("no type chosen", str(cm.exception).lower())

	def test_two_blank_rules_are_rejected_for_their_provider(self):
		"""Two empty leaves must surface the provider error rather than saving."""
		ch = self._new_channel()
		blank = {"status": "Active"}
		ch.member_rules_json = frappe.as_json({"conjunctions": ["or"], "conditions": [blank, blank]})
		with self.assertRaises(frappe.ValidationError) as cm:
			self._insert_under(ch, _FAKE)
		self.assertIn("no type chosen", str(cm.exception).lower())

	def test_rejects_an_unknown_provider(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._save_mapping_with_rule(
				_FAKE, label="x", provider="NOPE", rule_type="always-a", status="Active"
			)
		self.assertIn("NOPE", str(cm.exception))

	def test_rejects_a_rule_type_the_provider_does_not_declare(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._save_mapping_with_rule(
				_FAKE, label="x", provider="FAKE", rule_type="no-such", status="Active"
			)
		self.assertIn("no-such", str(cm.exception))

	def test_rejects_a_config_missing_a_required_field(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._save_mapping_with_rule(
				_REQCFG,
				label="x",
				provider="REQCFG",
				rule_type="needs-batch",
				status="Active",
				config="{}",
			)
		self.assertIn("Batch", str(cm.exception))

	def test_accepts_a_config_with_its_required_field(self):
		ch = self._save_mapping_with_rule(
			_REQCFG,
			label="x",
			provider="REQCFG",
			rule_type="needs-batch",
			status="Active",
			config=json.dumps({"batch": "B1"}),
		)
		self.assertEqual(json.loads(self._leaf(ch)["config"]), {"batch": "B1"})

	def test_channel_rejects_an_unknown_provider(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._save_channel_with_rule(
				_FAKE, label="x", provider="NOPE", rule_type="always-a", status="Active"
			)
		self.assertIn("NOPE", str(cm.exception))

	def test_channel_rejects_a_config_missing_a_required_field(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._save_channel_with_rule(
				_REQCFG,
				label="x",
				provider="REQCFG",
				rule_type="needs-batch",
				status="Active",
				config="{}",
			)
		self.assertIn("Batch", str(cm.exception))

	def test_label_is_not_schema_mandatory(self):
		# Naming is enforced by the write APIs (see test_rule_labels), not the schema:
		# a stored rule may sit unnamed, and saving its mapping must still work.
		ch = self._save_mapping_with_rule(_FAKE, provider="FAKE", rule_type="always-a", status="Active")
		self.assertFalse(self._leaf(ch).get("label"))
