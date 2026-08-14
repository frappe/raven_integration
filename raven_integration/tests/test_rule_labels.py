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


def _tree(*conditions, joiner="or"):
	return {"conjunctions": [joiner] * max(len(conditions) - 1, 0), "conditions": list(conditions)}


class TestRuleNameRequired(FrappeTestCase):
	"""A member rule must carry a name; the backend no longer invents one.

	Rules only exist on a channel mapping, so every case here goes through the
	channel endpoints — there is no workspace write that carries a rule any more.
	"""

	def setUp(self):
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			ws = frappe.new_doc("Raven Workspace Mapping")
			ws.workspace_label = f"Rule Name WS {frappe.generate_hash(length=6)}"
			ws.workspace_type = "Private"
			ws.flags.skip_raven_create = True
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
			ch.member_rules_json = frappe.as_json(_tree(_rule("Original Rule")))
			ch.insert()
		self.channel = ch.name
		self.channel_label = ch.channel_label
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", self.channel, force=True, ignore_missing=True)
		)

	def _channel_rule_labels(self):
		tree = frappe.parse_json(frappe.get_doc("Raven Channel Mapping", self.channel).member_rules_json)
		return [leaf.get("label") for leaf in tree["conditions"]]

	def test_update_channel_rejects_unnamed_rule(self):
		from raven_integration.api import update_channel

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.ValidationError) as cm:
				update_channel(
					name=self.channel,
					label=self.channel_label,
					type="Private",
					rules=_tree(_rule("")),
				)
		self.assertIn("Condition <b>1</b> has no name", str(cm.exception))
		self.assertEqual(self._channel_rule_labels(), ["Original Rule"])

	def test_update_channel_rejects_whitespace_only_rule_name(self):
		from raven_integration.api import update_channel

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.ValidationError):
				update_channel(
					name=self.channel,
					label=self.channel_label,
					type="Public",
					rules=_tree(_rule("   ")),
				)

	def test_rejected_unnamed_rule_writes_nothing(self):
		"""The throw lands before any field on the mapping is touched."""
		from raven_integration.api import update_channel

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.ValidationError):
				update_channel(
					name=self.channel,
					label="Renamed By A Failed Save",
					type="Public",
					rules=_tree(_rule("Renamed Rule"), _rule("", config={"tag": "b"})),
				)

		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.channel, "channel_type"),
			"Private",
		)
		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.channel, "channel_label"),
			self.channel_label,
		)
		self.assertEqual(self._channel_rule_labels(), ["Original Rule"])

	def test_diff_preview_accepts_an_unnamed_rule(self):
		"""A preview saves nothing and the name does not change who matches."""
		from raven_integration.api import compute_rule_diff

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			result = compute_rule_diff(
				target_doctype="Raven Channel Mapping",
				name=self.channel,
				new_rules=_tree(_rule("")),
			)
		self.assertIn("added", result)
		self.assertIn("removed", result)

	def test_diff_preview_rejects_a_workspace_target(self):
		# A workspace holds no rules, so there is no rule change to preview on one.
		from raven_integration.api import compute_rule_diff

		with self.assertRaises(frappe.ValidationError):
			compute_rule_diff(
				target_doctype="Raven Workspace Mapping",
				name=self.workspace,
				new_rules=_tree(_rule("Whatever")),
			)

	def test_update_channel_saves_a_named_rule(self):
		from raven_integration.api import get_channel, update_channel

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			new_name = update_channel(
				name=self.channel,
				label=self.channel_label,
				type="Private",
				rules=_tree(_rule("  Trainers of Vue Basics  ")),
			)
		self.assertEqual(new_name, self.channel)
		# Stored, trimmed, and served back to the UI.
		self.assertEqual(self._channel_rule_labels(), ["Trainers of Vue Basics"])
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			served = get_channel(self.channel)["rules"]
		self.assertEqual([r["label"] for r in served["conditions"]], ["Trainers of Vue Basics"])

	def test_pausing_survives_an_unnamed_stored_rule(self):
		# The status endpoint writes the one leaf rather than saving the doc, so a
		# stored rule left unnamed by the migration patch can still be paused and
		# resumed — the save path's name check would reject it outright.
		from raven_integration.api import set_channel_rule_status

		tree = frappe.parse_json(
			frappe.db.get_value("Raven Channel Mapping", self.channel, "member_rules_json")
		)
		tree["conditions"][0]["label"] = ""
		frappe.db.set_value("Raven Channel Mapping", self.channel, "member_rules_json", frappe.as_json(tree))
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			result = set_channel_rule_status(self.channel, [0], "Paused")
		self.assertEqual(result, {"status": "Paused"})

	def test_update_workspace_takes_no_rules(self):
		# The signature is the contract: a caller that still sends rules to a
		# workspace gets a TypeError, not a silent drop.
		from raven_integration.api import update_workspace

		with self.assertRaises(TypeError):
			update_workspace(
				name=self.workspace,
				label=self.workspace_label,
				type="Private",
				rules=[_rule("Nope")],
			)
