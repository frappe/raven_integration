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


class TestRavenMembershipRule(FrappeTestCase):
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
		channel-specific cases below exercise."""
		ch = self._new_channel()
		ch.append("member_rules", rule)
		self._insert_under(ch, provider_paths)
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", ch.name, force=True, ignore_missing=True)
		)
		return ch

	_save_channel_with_rule = _save_mapping_with_rule

	def test_stores_provider_type_and_opaque_config(self):
		ch = self._save_mapping_with_rule(
			_FAKE,
			label="My Rule",
			provider="FAKE",
			rule_type="always-ab",
			status="Active",
			config=json.dumps({"courses": ["C1"]}),
		)
		rule = ch.member_rules[0]
		self.assertEqual(rule.provider, "FAKE")
		self.assertEqual(json.loads(rule.config), {"courses": ["C1"]})

	def test_requires_provider_and_rule_type(self):
		"""The parent's own mandatory check covers its child rows' reqd fields."""
		with self.assertRaises(frappe.exceptions.MandatoryError):
			self._save_mapping_with_rule(_FAKE, label="x", status="Active")

	def test_two_blank_rules_raise_mandatory_not_duplicate(self):
		"""Two empty rows must surface the mandatory error, not a spurious duplicate."""
		ch = self._new_channel()
		ch.append("member_rules", {"status": "Active"})
		ch.append("member_rules", {"status": "Active"})
		with self.assertRaises(frappe.exceptions.MandatoryError):
			self._insert_under(ch, _FAKE)

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
		self.assertEqual(json.loads(ch.member_rules[0].config), {"batch": "B1"})

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
		self.assertFalse(ch.member_rules[0].label)
