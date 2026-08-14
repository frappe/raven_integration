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
		# Sibling rules are deduplicated on (provider, rule_type, config), so tests
		# that need two rules of one type vary the config.
		"config": config if config is not None else {},
	}


def _tree(*conditions, joiner="or"):
	return {"conjunctions": [joiner] * max(len(conditions) - 1, 0), "conditions": list(conditions)}


class TestSetRuleStatus(FrappeTestCase):
	"""set_channel_rule_status — the escape hatch that re-enables a rule the
	ordinary save path can no longer reach.

	A condition has no docname now, so it is addressed by its path: the child
	indices from the root of the channel's tree. Two channels under one workspace:
	the second is what proves one mapping's conditions are not reachable through
	another.
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

		self.channel = self._make_channel(
			"Rule Status CH", _tree(_rule("First Rule"), _rule("Second Rule", config={"tag": "b"}))
		)
		self.other_channel = self._make_channel("Rule Status Other CH", _tree(_rule("Other Channel Rule")))

	def _make_channel(self, label, tree):
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			ch = frappe.new_doc("Raven Channel Mapping")
			ch.channel_label = f"{label} {frappe.generate_hash(length=6)}"
			ch.workspace = self.workspace
			ch.channel_type = "Private"
			ch.flags.skip_raven_create = True
			ch.member_rules_json = frappe.as_json(tree)
			ch.insert()
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", ch.name, force=True, ignore_missing=True)
		)
		return ch.name

	def _leaf(self, channel, path):
		node = frappe.parse_json(frappe.db.get_value("Raven Channel Mapping", channel, "member_rules_json"))
		for index in path:
			node = node["conditions"][index]
		return node

	def _status(self, channel, path):
		return self._leaf(channel, path)["status"]

	def test_channel_rule_toggles_active_to_paused_and_back(self):
		from raven_integration.api import set_channel_rule_status

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			result = set_channel_rule_status(self.channel, [0], "Paused")
		self.assertEqual(result, {"status": "Paused"})
		self.assertEqual(self._status(self.channel, [0]), "Paused")

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			result = set_channel_rule_status(self.channel, [0], "Active")
		self.assertEqual(result, {"status": "Active"})
		self.assertEqual(self._status(self.channel, [0]), "Active")

	def test_accepts_a_path_sent_as_json(self):
		"""The frontend sends it over HTTP, where a list arrives as a JSON string."""
		from raven_integration.api import set_channel_rule_status

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			set_channel_rule_status(self.channel, "[1]", "Paused")
		self.assertEqual(self._status(self.channel, [1]), "Paused")

	def test_pauses_a_leaf_inside_a_nested_group(self):
		from raven_integration.api import set_channel_rule_status

		channel = self._make_channel(
			"Rule Status Nested CH",
			_tree(_rule("Top"), _tree(_rule("Inner"), _rule("Inner Two", config={"tag": "b"}))),
		)
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			set_channel_rule_status(channel, [1, 0], "Paused")
		self.assertEqual(self._status(channel, [1, 0]), "Paused")
		self.assertEqual(self._status(channel, [1, 1]), "Active")

	def test_refuses_to_pause_a_rule_under_and(self):
		"""Pausing freezes a contribution, which only reads as "adds nobody new" while
		the rule adds people. Under `and` it narrows, so dropping it would add them."""
		from raven_integration.api import set_channel_rule_status

		channel = self._make_channel(
			"Rule Status And CH",
			_tree(_rule("A"), _rule("B", config={"tag": "b"}), joiner="and"),
		)
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.ValidationError) as cm:
				set_channel_rule_status(channel, [0], "Paused")
		self.assertIn("sits in a group joined by", str(cm.exception).lower())
		self.assertEqual(self._status(channel, [0]), "Active")

	def test_re_enables_a_paused_unnamed_rule(self):
		"""The whole point: a Paused + unnamed rule is frozen in the UI and rejected
		by the save path, so this endpoint is its only route back to Active."""
		from raven_integration.api import set_channel_rule_status

		tree = frappe.parse_json(
			frappe.db.get_value("Raven Channel Mapping", self.channel, "member_rules_json")
		)
		tree["conditions"][0].update({"label": "", "status": "Paused"})
		frappe.db.set_value("Raven Channel Mapping", self.channel, "member_rules_json", frappe.as_json(tree))

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			result = set_channel_rule_status(self.channel, [0], "Active")

		self.assertEqual(result, {"status": "Active"})
		self.assertEqual(self._status(self.channel, [0]), "Active")
		# Still unnamed — this endpoint changes status and nothing else.
		self.assertEqual(self._leaf(self.channel, [0])["label"], "")

	def test_a_path_is_resolved_against_this_mapping_only(self):
		"""Position [0] exists on both channels; the write must land on this one."""
		from raven_integration.api import set_channel_rule_status

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			set_channel_rule_status(self.channel, [0], "Paused")
		self.assertEqual(self._status(self.other_channel, [0]), "Active")

	def test_rejects_a_path_that_is_not_there(self):
		from raven_integration.api import set_channel_rule_status

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.DoesNotExistError) as cm:
				set_channel_rule_status(self.channel, [7], "Paused")
		self.assertIn("No condition at position", str(cm.exception))

	def test_rejects_a_path_addressing_a_group(self):
		"""A group has no status of its own; only a leaf can be paused."""
		from raven_integration.api import set_channel_rule_status

		channel = self._make_channel("Rule Status Group CH", _tree(_rule("Top"), _tree(_rule("Inner"))))
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.DoesNotExistError):
				set_channel_rule_status(channel, [1], "Paused")

	def test_rejects_a_malformed_path(self):
		from raven_integration.api import set_channel_rule_status

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			for bad in ([], "nonsense", [-1], ["0"], {"0": 1}, [True]):
				with self.assertRaises(frappe.ValidationError):
					set_channel_rule_status(self.channel, bad, "Paused")
		self.assertEqual(self._status(self.channel, [0]), "Active")

	def test_rejects_an_unknown_mapping(self):
		from raven_integration.api import set_channel_rule_status

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.DoesNotExistError):
				set_channel_rule_status("RCM-No Such Channel", [0], "Paused")
		self.assertEqual(self._status(self.channel, [0]), "Active")

	def test_rejects_an_invalid_status(self):
		from raven_integration.api import set_channel_rule_status

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.ValidationError) as cm:
				set_channel_rule_status(self.channel, [0], "Disabled")
		self.assertIn("Active, Paused", str(cm.exception))
		self.assertEqual(self._status(self.channel, [0]), "Active")

	def test_rejects_a_non_string_status(self):
		"""Called through the shared body: the whitelist wrapper's own type coercion
		would raise FrappeTypeError first and hide the endpoint's explicit guard."""
		from raven_integration.api import _set_rule_status

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.ValidationError) as cm:
				_set_rule_status("Raven Channel Mapping", self.channel, [0], {"status": "Paused"})
		self.assertIn("must be text", str(cm.exception))
		self.assertEqual(self._status(self.channel, [0]), "Active")

	def test_leaves_sibling_rules_and_the_mapping_untouched(self):
		from raven_integration.api import set_channel_rule_status

		fields = ["channel_label", "channel_type", "enabled", "stale"]
		before = frappe.db.get_value("Raven Channel Mapping", self.channel, fields, as_dict=True)
		sibling_before = self._leaf(self.channel, [1])

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			set_channel_rule_status(self.channel, [0], "Paused")

		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.channel, fields, as_dict=True), before
		)
		self.assertEqual(self._leaf(self.channel, [1]), sibling_before)
		# Both conditions still there — nothing was replaced or dropped.
		tree = frappe.parse_json(
			frappe.db.get_value("Raven Channel Mapping", self.channel, "member_rules_json")
		)
		self.assertEqual(len(tree["conditions"]), 2)
		self.assertEqual(tree["conjunctions"], ["or"])
		# Only the target leaf's own fields other than status are preserved.
		self.assertEqual(self._leaf(self.channel, [0])["label"], "First Rule")

	def test_schedules_a_resync(self):
		"""Membership has to follow the status change, exactly as update_* does."""
		from raven_integration import api

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with patch.object(api, "_schedule_resync") as scheduled:
				api.set_channel_rule_status(self.channel, [0], "Paused")
		scheduled.assert_called_once()

	def test_channel_endpoint_requires_system_manager(self):
		from raven_integration.api import set_channel_rule_status

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				set_channel_rule_status(self.channel, [0], "Paused")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self._status(self.channel, [0]), "Active")

	def test_there_is_no_workspace_rule_status_endpoint(self):
		# A workspace holds no rules, so the endpoint that paused one is gone.
		from raven_integration import api

		self.assertFalse(hasattr(api, "set_workspace_rule_status"))
		self.assertFalse(hasattr(api, "set_workspace_combinator"))

	def test_there_is_no_combinator_endpoint_left(self):
		# A mapping-wide combinator is gone: a group carries its own conjunction.
		from raven_integration import api

		self.assertFalse(hasattr(api, "set_channel_combinator"))
