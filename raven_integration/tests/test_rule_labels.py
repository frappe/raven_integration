from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import registry

_FAKE = ["raven_integration.tests.fake_provider.get_provider"]


def _rule(label, rule_type="always-ab", config=None):
	return {
		"label": label,
		"provider": "FAKE",
		"rule_type": rule_type,
		"status": "Active",
		# Rules are deduplicated on (provider, rule_type, config), so tests that
		# need two rules of one type vary the config.
		"config": config if config is not None else {},
	}


class TestRuleNameRequired(FrappeTestCase):
	"""A member rule must carry a name; the backend no longer invents one."""

	def setUp(self):
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			ws = frappe.new_doc("Raven Workspace Mapping")
			ws.workspace_label = f"Rule Name WS {frappe.generate_hash(length=6)}"
			ws.workspace_type = "Private"
			ws.flags.skip_raven_create = True
			ws.append("member_rules", _rule("Original Rule"))
			ws.insert()
		self.workspace = ws.name
		self.workspace_label = ws.workspace_label
		self.addCleanup(
			lambda: frappe.delete_doc(
				"Raven Workspace Mapping", self.workspace, force=True, ignore_missing=True
			)
		)

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			ch = frappe.new_doc("Raven Channel Mapping")
			ch.channel_label = f"Rule Name CH {frappe.generate_hash(length=6)}"
			ch.workspace = self.workspace
			ch.channel_type = "Private"
			ch.flags.skip_raven_create = True
			ch.append("member_rules", _rule("Original Rule"))
			ch.insert()
		self.channel = ch.name
		self.channel_label = ch.channel_label
		self.addCleanup(
			lambda: frappe.delete_doc(
				"Raven Channel Mapping", self.channel, force=True, ignore_missing=True
			)
		)

	def _workspace_rule_labels(self):
		return [
			r.label for r in frappe.get_doc("Raven Workspace Mapping", self.workspace).member_rules
		]

	def _channel_rule_labels(self):
		return [
			r.label for r in frappe.get_doc("Raven Channel Mapping", self.channel).member_rules
		]

	def test_update_workspace_rejects_unnamed_rule(self):
		from raven_integration.api import update_workspace

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.ValidationError) as cm:
				update_workspace(
					name=self.workspace,
					label=self.workspace_label,
					type="Public",
					rules=[_rule("")],
				)
		self.assertIn("Row #1 of <b>Member Rules</b> has no name", str(cm.exception))

	def test_update_workspace_rejects_whitespace_only_rule_name(self):
		from raven_integration.api import update_workspace

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.ValidationError):
				update_workspace(
					name=self.workspace,
					label=self.workspace_label,
					type="Public",
					rules=[_rule("   ")],
				)

	def test_rejected_unnamed_rule_writes_nothing(self):
		"""The throw lands before any field on the mapping is touched."""
		from raven_integration.api import update_workspace

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.ValidationError):
				update_workspace(
					name=self.workspace,
					label="Renamed By A Failed Save",
					type="Public",
					rules=[_rule("Renamed Rule"), _rule("", config={"tag": "b"})],
				)

		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.workspace, "workspace_type"),
			"Private",
		)
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.workspace, "workspace_label"),
			self.workspace_label,
		)
		self.assertEqual(self._workspace_rule_labels(), ["Original Rule"])

	def test_update_channel_rejects_unnamed_rule(self):
		from raven_integration.api import update_channel

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.ValidationError) as cm:
				update_channel(
					name=self.channel,
					label=self.channel_label,
					type="Private",
					rules=[_rule("")],
				)
		self.assertIn("Row #1 of <b>Member Rules</b> has no name", str(cm.exception))
		self.assertEqual(self._channel_rule_labels(), ["Original Rule"])

	def test_diff_preview_accepts_an_unnamed_rule(self):
		"""A preview saves nothing and the name does not change who matches."""
		from raven_integration.api import compute_rule_diff

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			result = compute_rule_diff(
				target_doctype="Raven Workspace Mapping",
				name=self.workspace,
				new_rules=[_rule("")],
			)
		self.assertIn("added", result)
		self.assertIn("removed", result)

	def test_update_workspace_saves_a_named_rule(self):
		from raven_integration.api import get_workspace, update_workspace

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			new_name = update_workspace(
				name=self.workspace,
				label=self.workspace_label,
				type="Private",
				rules=[_rule("  Trainers of Vue Basics  ")],
			)
		self.assertEqual(new_name, self.workspace)
		# Stored, trimmed, and served back to the UI.
		self.assertEqual(self._workspace_rule_labels(), ["Trainers of Vue Basics"])
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			served = get_workspace(self.workspace)["member_rules"]
		self.assertEqual([r["label"] for r in served], ["Trainers of Vue Basics"])

	def test_combinator_switch_survives_an_unnamed_stored_rule(self):
		# set_*_combinator saves the whole mapping doc, so a `reqd` label would make
		# the patch's own blanked rules raise MandatoryError on every AND/OR switch.
		from raven_integration.api import set_workspace_combinator

		frappe.db.set_value(
			"Raven Membership Rule",
			frappe.get_doc("Raven Workspace Mapping", self.workspace).member_rules[0].name,
			"label",
			"",
			update_modified=False,
		)
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			result = set_workspace_combinator(self.workspace, "All (AND)")
		self.assertEqual(result["combinator"], "All (AND)")
