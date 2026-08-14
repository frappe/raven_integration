from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import registry

_FAKE = ["raven_integration.tests.fake_provider.get_provider"]


def _rule(label, rule_type="always-ab", config=None, status="Active"):
	return {
		"label": label,
		"provider": "FAKE",
		"rule_type": rule_type,
		"status": status,
		# Rules are deduplicated on (provider, rule_type, config), so tests that
		# need two rules of one type vary the config.
		"config": config if config is not None else {},
	}


class TestSetRuleStatus(FrappeTestCase):
	"""set_channel_rule_status — the escape hatch that re-enables a rule the
	ordinary save path can no longer reach.

	Two channels under one workspace: the second is what proves a rule of one
	mapping is not reachable through another.
	"""

	def setUp(self):
		self.non_admin = frappe.get_doc(
			{
				"doctype": "User",
				"email": "rv-rule-status-non-admin@example.com",
				"first_name": "RuleStatusPlain",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(self.non_admin.delete)

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			ws = frappe.new_doc("Raven Workspace Mapping")
			ws.workspace_label = f"Rule Status WS {frappe.generate_hash(length=6)}"
			ws.workspace_type = "Private"
			ws.flags.skip_raven_create = True
			ws.insert()
		self.workspace = ws.name
		self.addCleanup(
			lambda: frappe.delete_doc(
				"Raven Workspace Mapping", self.workspace, force=True, ignore_missing=True
			)
		)

		self.channel, self.ch_rules = self._make_channel(
			"Rule Status CH", [_rule("First Rule"), _rule("Second Rule", config={"tag": "b"})]
		)
		self.other_channel, self.other_rules = self._make_channel(
			"Rule Status Other CH", [_rule("Other Channel Rule")]
		)

	def _make_channel(self, label, rules):
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			ch = frappe.new_doc("Raven Channel Mapping")
			ch.channel_label = f"{label} {frappe.generate_hash(length=6)}"
			ch.workspace = self.workspace
			ch.channel_type = "Private"
			ch.flags.skip_raven_create = True
			for rule in rules:
				ch.append("member_rules", rule)
			ch.insert()
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", ch.name, force=True, ignore_missing=True)
		)
		return ch.name, [r.name for r in ch.member_rules]

	def _status(self, rule):
		return frappe.db.get_value("Raven Membership Rule", rule, "status")

	def test_channel_rule_toggles_active_to_paused_and_back(self):
		from raven_integration.api import set_channel_rule_status

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			result = set_channel_rule_status(self.channel, self.ch_rules[0], "Paused")
		self.assertEqual(result, {"status": "Paused"})
		self.assertEqual(self._status(self.ch_rules[0]), "Paused")

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			result = set_channel_rule_status(self.channel, self.ch_rules[0], "Active")
		self.assertEqual(result, {"status": "Active"})
		self.assertEqual(self._status(self.ch_rules[0]), "Active")

	def test_re_enables_a_paused_unnamed_rule(self):
		"""The whole point: a Paused + unnamed rule is frozen in the UI and rejected
		by the save path, so this endpoint is its only route back to Active."""
		from raven_integration.api import set_channel_rule_status

		frappe.db.set_value(
			"Raven Membership Rule",
			self.ch_rules[0],
			{"label": "", "status": "Paused"},
			update_modified=False,
		)

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			result = set_channel_rule_status(self.channel, self.ch_rules[0], "Active")

		self.assertEqual(result, {"status": "Active"})
		self.assertEqual(self._status(self.ch_rules[0]), "Active")
		# Still unnamed — this endpoint changes status and nothing else.
		self.assertEqual(frappe.db.get_value("Raven Membership Rule", self.ch_rules[0], "label"), "")

	def test_rejects_a_rule_belonging_to_another_mapping(self):
		"""A sibling channel's rule row must not be reachable through this channel."""
		from raven_integration.api import set_channel_rule_status

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.DoesNotExistError) as cm:
				set_channel_rule_status(self.channel, self.other_rules[0], "Paused")
		self.assertIn("No member rule", str(cm.exception))
		self.assertEqual(self._status(self.other_rules[0]), "Active")

	def test_rejects_an_unknown_rule(self):
		from raven_integration.api import set_channel_rule_status

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.DoesNotExistError):
				set_channel_rule_status(self.channel, "no-such-rule-row", "Paused")

	def test_rejects_an_unknown_mapping(self):
		from raven_integration.api import set_channel_rule_status

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.DoesNotExistError):
				set_channel_rule_status("RCM-No Such Channel", self.ch_rules[0], "Paused")
		self.assertEqual(self._status(self.ch_rules[0]), "Active")

	def test_rejects_an_invalid_status(self):
		from raven_integration.api import set_channel_rule_status

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.ValidationError) as cm:
				set_channel_rule_status(self.channel, self.ch_rules[0], "Disabled")
		self.assertIn("Active, Paused", str(cm.exception))
		self.assertEqual(self._status(self.ch_rules[0]), "Active")

	def test_rejects_a_non_string_status(self):
		"""Called through the shared body: the whitelist wrapper's own type coercion
		would raise FrappeTypeError first and hide the endpoint's explicit guard."""
		from raven_integration.api import _set_rule_status

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.ValidationError) as cm:
				_set_rule_status(
					"Raven Channel Mapping",
					self.channel,
					self.ch_rules[0],
					{"status": "Paused"},
				)
		self.assertIn("must be text", str(cm.exception))
		self.assertEqual(self._status(self.ch_rules[0]), "Active")

	def test_leaves_sibling_rules_and_the_mapping_untouched(self):
		from raven_integration.api import set_channel_rule_status

		fields = ["channel_label", "channel_type", "rule_combinator", "enabled", "stale"]
		before = frappe.db.get_value("Raven Channel Mapping", self.channel, fields, as_dict=True)
		sibling_before = frappe.db.get_value(
			"Raven Membership Rule",
			self.ch_rules[1],
			["label", "status", "provider", "rule_type", "config", "modified"],
			as_dict=True,
		)

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			set_channel_rule_status(self.channel, self.ch_rules[0], "Paused")

		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.channel, fields, as_dict=True),
			before,
		)
		self.assertEqual(
			frappe.db.get_value(
				"Raven Membership Rule",
				self.ch_rules[1],
				["label", "status", "provider", "rule_type", "config", "modified"],
				as_dict=True,
			),
			sibling_before,
		)
		# Both rows still there — nothing was replaced or deleted.
		self.assertEqual(
			[r.name for r in frappe.get_doc("Raven Channel Mapping", self.channel).member_rules],
			self.ch_rules,
		)
		# Only the target row's own fields other than status are preserved.
		self.assertEqual(
			frappe.db.get_value("Raven Membership Rule", self.ch_rules[0], "label"), "First Rule"
		)

	def test_schedules_a_resync(self):
		"""Membership has to follow the status change, exactly as update_* does."""
		from raven_integration import api

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with patch.object(api, "_schedule_resync") as scheduled:
				api.set_channel_rule_status(self.channel, self.ch_rules[0], "Paused")
		scheduled.assert_called_once()

	def test_channel_endpoint_requires_system_manager(self):
		from raven_integration.api import set_channel_rule_status

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				set_channel_rule_status(self.channel, self.ch_rules[0], "Paused")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self._status(self.ch_rules[0]), "Active")

	def test_there_is_no_workspace_rule_status_endpoint(self):
		# A workspace holds no rules, so the endpoint that paused one is gone.
		from raven_integration import api

		self.assertFalse(hasattr(api, "set_workspace_rule_status"))
		self.assertFalse(hasattr(api, "set_workspace_combinator"))
