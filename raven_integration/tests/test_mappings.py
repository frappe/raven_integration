from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import registry

_FAKE = ["raven_integration.tests.fake_provider.get_provider"]


class TestRavenWorkspaceMapping(FrappeTestCase):
	def test_workspace_mapping_carries_no_rules(self):
		# Rules live on channels only; a workspace's membership is derived from them.
		meta = frappe.get_meta("Raven Workspace Mapping")
		f = {df.fieldname: df for df in meta.fields}
		self.assertNotIn("member_rules", f)
		self.assertNotIn("rule_combinator", f)


class TestRavenChannelMapping(FrappeTestCase):
	def test_channel_mapping_links_to_workspace_mapping(self):
		meta = frappe.get_meta("Raven Channel Mapping")
		f = {df.fieldname: df for df in meta.fields}
		self.assertEqual(f["workspace"].options, "Raven Workspace Mapping")
		self.assertIn("member_rules", f)
		self.assertEqual(f["member_rules"].options, "Raven Membership Rule")


class TestWorkspaceDeleteCascade(FrappeTestCase):
	"""Deleting a workspace mapping cascade-deletes its child channel mappings,
	so the framework's LinkExistsError no longer fires."""

	def _make_workspace(self, label):
		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = label
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		return ws

	def _make_channel(self, workspace, label):
		ch = frappe.new_doc("Raven Channel Mapping")
		ch.channel_label = label
		ch.workspace = workspace
		ch.channel_type = "Private"
		ch.flags.skip_raven_create = True
		ch.insert()
		return ch

	def test_deleting_workspace_with_channels_cascades(self):
		ws = self._make_workspace("Cascade Test WS")
		ch1 = self._make_channel(ws.name, "Cascade Test Channel 1")
		ch2 = self._make_channel(ws.name, "Cascade Test Channel 2")
		# Belt-and-braces: if the cascade ever regresses, clean up by hand.
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", ch1.name, force=True, ignore_missing=True)
		)
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", ch2.name, force=True, ignore_missing=True)
		)
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", ws.name, force=True, ignore_missing=True)
		)

		# Should not raise LinkExistsError despite the child channels linking back.
		frappe.delete_doc("Raven Workspace Mapping", ws.name)

		self.assertFalse(frappe.db.exists("Raven Workspace Mapping", ws.name))
		self.assertFalse(frappe.db.exists("Raven Channel Mapping", ch1.name))
		self.assertFalse(frappe.db.exists("Raven Channel Mapping", ch2.name))

	def test_deleting_channel_alone(self):
		ws = self._make_workspace("Channel-Only Delete WS")
		ch = self._make_channel(ws.name, "Lone Channel")
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", ws.name, force=True, ignore_missing=True)
		)
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", ch.name, force=True, ignore_missing=True)
		)

		frappe.delete_doc("Raven Channel Mapping", ch.name)

		self.assertFalse(frappe.db.exists("Raven Channel Mapping", ch.name))
		self.assertTrue(frappe.db.exists("Raven Workspace Mapping", ws.name))


class TestDuplicateRuleGuard(FrappeTestCase):
	"""A channel mapping rejects two rules with the same provider/rule_type/config —
	provider-agnostic duplicate detection."""

	def setUp(self):
		self.workspace = frappe.new_doc("Raven Workspace Mapping")
		self.workspace.workspace_label = f"Dup Rule WS {frappe.generate_hash(length=6)}"
		self.workspace.workspace_type = "Private"
		self.workspace.flags.skip_raven_create = True
		self.workspace.insert()
		self.addCleanup(
			lambda: frappe.delete_doc(
				"Raven Workspace Mapping", self.workspace.name, force=True, ignore_missing=True
			)
		)

	def _rule(self, label, rule_type="always-a"):
		return {
			"label": label,
			"provider": "FAKE",
			"rule_type": rule_type,
			"status": "Active",
			"config": {},
		}

	def _channel(self, label):
		ch = frappe.new_doc("Raven Channel Mapping")
		ch.channel_label = f"{label} {frappe.generate_hash(length=6)}"
		ch.workspace = self.workspace.name
		ch.channel_type = "Private"
		ch.flags.skip_raven_create = True
		return ch

	def test_identical_rules_rejected(self):
		ch = self._channel("Dup Rule Channel")
		# Same selection criteria twice (labels differ, but that is cosmetic).
		ch.append("member_rules", self._rule("First"))
		ch.append("member_rules", self._rule("Second"))
		with patch.object(registry, "_provider_paths", lambda: _FAKE):
			with self.assertRaises(frappe.ValidationError):
				ch.insert()

	def test_distinct_rules_allowed(self):
		ch = self._channel("Distinct Rule Channel")
		ch.append("member_rules", self._rule("A", rule_type="always-a"))
		ch.append("member_rules", self._rule("B", rule_type="always-ab"))
		with patch.object(registry, "_provider_paths", lambda: _FAKE):
			ch.insert()
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", ch.name, force=True, ignore_missing=True)
		)
		self.assertEqual(len(ch.member_rules), 2)


class TestRavenDeleteNotBlocked(FrappeTestCase):
	"""The mappings' Link to Raven Workspace/Channel must NOT block Raven's own
	delete — guaranteed by the ignore_links_on_delete hook."""

	def setUp(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")

	def test_hook_exempts_both_mappings(self):
		hooks = frappe.get_hooks("ignore_links_on_delete")
		self.assertIn("Raven Workspace Mapping", hooks)
		self.assertIn("Raven Channel Mapping", hooks)

	def test_deleting_linked_raven_workspace_succeeds(self):
		suffix = frappe.generate_hash(length=6)
		raven_ws = frappe.get_doc(
			{
				"doctype": "Raven Workspace",
				"workspace_name": f"Del WS {suffix}",
				"type": "Private",
			}
		).insert(ignore_permissions=True)

		ws_map = frappe.new_doc(
			"Raven Workspace Mapping",
			workspace_label=f"Del WS Map {suffix}",
			workspace_type="Private",
		)
		ws_map.flags.skip_raven_create = True
		ws_map.insert()
		frappe.db.set_value("Raven Workspace Mapping", ws_map.name, "raven_workspace", raven_ws.name)
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", ws_map.name, force=True, ignore_missing=True)
		)

		# Must NOT raise LinkExistsError despite the mapping linking to it.
		frappe.delete_doc("Raven Workspace", raven_ws.name)
		self.assertFalse(frappe.db.exists("Raven Workspace", raven_ws.name))
