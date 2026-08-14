from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import engine, registry

_FAKE = ["raven_integration.tests.fake_provider.get_provider"]


def _derived(members):
	"""Patch the channel-membership read a workspace's population is derived from."""
	return patch("raven_integration.engine.members_of_workspace_channels", return_value=set(members))


class TestWorkspacePopulationCache(FrappeTestCase):
	def setUp(self):
		self._link = patch("raven_integration.engine.frappe.db.get_value", return_value="raven-ws")
		self._link.start()
		self.addCleanup(self._link.stop)

	def test_population_memoized_per_cache(self):
		cache: dict = {}
		with _derived({"a@example.com"}) as read:
			r1 = engine.expected_workspace_members("WS-X", cache=cache)
			r2 = engine.expected_workspace_members("WS-X", cache=cache)
		self.assertEqual(read.call_count, 1, "second lookup must be served from the cache")
		self.assertEqual(r1, r2)

	def test_no_cache_reevaluates(self):
		with _derived(set()) as read:
			engine.expected_workspace_members("WS-X")
			engine.expected_workspace_members("WS-X")
		self.assertEqual(read.call_count, 2, "without a cache each call re-reads")


class TestDerivedWorkspaceMembership(FrappeTestCase):
	def test_members_are_whoever_is_in_a_channel(self):
		with (
			patch("raven_integration.engine.frappe.db.get_value", return_value="raven-ws"),
			_derived({"a@example.com", "b@example.com"}),
		):
			self.assertEqual(
				engine.expected_workspace_members("WS-X"),
				{"a@example.com", "b@example.com"},
			)

	def test_unlinked_workspace_has_no_members(self):
		# No backing Raven workspace means no channels to read, so nobody is derived —
		# and the channel read is never reached.
		with (
			patch("raven_integration.engine.frappe.db.get_value", return_value=None),
			_derived({"a@example.com"}) as read,
		):
			self.assertEqual(engine.expected_workspace_members("WS-X"), set())
		read.assert_not_called()


def _rule(rule_type, status="Active"):
	return {"provider": "FAKE", "rule_type": rule_type, "config": {}, "status": status}


def _tree(*conditions, joiner="or"):
	"""A root group holding these conditions, joined by one conjunction throughout."""
	return {
		"conjunctions": [joiner] * max(len(conditions) - 1, 0),
		"conditions": list(conditions),
	}


class TestEngineRuleCombination(FrappeTestCase):
	def setUp(self):
		self._patch = patch.object(registry, "_provider_paths", lambda: _FAKE)
		self._patch.start()
		self.addCleanup(self._patch.stop)

	def test_or_unions_two_rule_populations(self):
		tree = _tree(_rule("always-a"), _rule("always-ab"), joiner="or")
		self.assertEqual(engine.evaluate_rules(tree), {"a@example.com", "b@example.com"})

	def test_and_intersects_two_rule_populations(self):
		tree = _tree(_rule("always-a"), _rule("always-ab"), joiner="and")
		self.assertEqual(engine.evaluate_rules(tree), {"a@example.com"})

	def test_paused_rules_are_ignored(self):
		tree = _tree(_rule("always-ab"), _rule("always-a", status="Paused"), joiner="and")
		# The paused always-a rule is absent, so it does not narrow the result.
		self.assertEqual(engine.evaluate_rules(tree), {"a@example.com", "b@example.com"})

	def test_no_active_rules_means_nobody(self):
		self.assertEqual(engine.evaluate_rules(_tree(_rule("always-a", status="Paused"))), set())

	def test_a_stored_json_string_is_read(self):
		# What the DB column actually holds; every read path takes it as it comes.
		import json

		tree = json.dumps(_tree(_rule("always-a")))
		self.assertEqual(engine.evaluate_rules(tree), {"a@example.com"})

	def test_nested_group_is_intersected_with_its_sibling(self):
		# always-ab and (always-a or always-ab) — the nesting the flat model could not say.
		tree = _tree(
			_rule("always-ab"),
			_tree(_rule("always-a"), _rule("always-ab"), joiner="or"),
			joiner="and",
		)
		self.assertEqual(engine.evaluate_rules(tree), {"a@example.com", "b@example.com"})

	def test_nested_group_narrows_when_it_matches_less(self):
		tree = _tree(
			_rule("always-ab"),
			_tree(_rule("always-a"), joiner="or"),
			joiner="and",
		)
		self.assertEqual(engine.evaluate_rules(tree), {"a@example.com"})

	def test_and_binds_tighter_than_or_on_a_mixed_level(self):
		# always-ab or (always-a and always-a). A level this UI never writes, but a
		# stored tree is evaluated with Python's precedence rather than left to right —
		# folding left to right would intersect first and answer {a} instead.
		tree = {
			"conjunctions": ["or", "and"],
			"conditions": [_rule("always-ab"), _rule("always-a"), _rule("always-a")],
		}
		self.assertEqual(engine.evaluate_rules(tree), {"a@example.com", "b@example.com"})

	def test_two_and_runs_are_unioned_across_an_or(self):
		# (always-ab and always-ab) or (always-a and always-a) — the four-leaf shape,
		# where precedence has to hold on both sides of the or rather than just after it.
		tree = {
			"conjunctions": ["and", "or", "and"],
			"conditions": [
				_rule("always-ab"),
				_rule("always-ab"),
				_rule("always-a"),
				_rule("always-a"),
			],
		}
		self.assertEqual(engine.evaluate_rules(tree), {"a@example.com", "b@example.com"})

	def test_an_absent_child_keeps_the_remaining_gaps_lined_up(self):
		# The paused rule drops out and takes its own leading gap with it, so the
		# survivors still read the right conjunctions rather than sliding out of step.
		tree = {
			"conjunctions": ["and", "or"],
			"conditions": [
				_rule("always-ab"),
				_rule("always-a", status="Paused"),
				_rule("always-a"),
			],
		}
		self.assertEqual(engine.evaluate_rules(tree), {"a@example.com", "b@example.com"})

	def test_a_mixed_fold_still_binds_and_tighter_after_a_child_drops_out(self):
		# The paused rule sits between the two and-runs and takes conjunctions[1]
		# with it, leaving and/or/and across the survivors. Precedence answers
		# {a, b}; folding left to right would intersect first and answer {a}.
		tree = {
			"conjunctions": ["and", "or", "or", "and"],
			"conditions": [
				_rule("always-ab"),
				_rule("always-ab"),
				_rule("always-a", status="Paused"),
				_rule("always-a"),
				_rule("always-a"),
			],
		}
		self.assertEqual(engine.evaluate_rules(tree), {"a@example.com", "b@example.com"})

	def test_an_empty_group_does_not_narrow_its_parent(self):
		# A group with nothing in it is absent, not "matches nobody": intersecting
		# against it would empty the channel.
		tree = _tree(_rule("always-ab"), _tree(joiner="and"), joiner="and")
		self.assertEqual(engine.evaluate_rules(tree), {"a@example.com", "b@example.com"})

	def test_a_group_of_only_paused_rules_is_absent_too(self):
		tree = _tree(
			_rule("always-ab"),
			_tree(_rule("always-a", status="Paused")),
			joiner="and",
		)
		self.assertEqual(engine.evaluate_rules(tree), {"a@example.com", "b@example.com"})

	def test_nothing_at_all_is_nobody(self):
		self.assertEqual(engine.evaluate_rules(None), set())
		self.assertEqual(engine.evaluate_rules(_tree()), set())

	def test_channel_members_are_not_narrowed_by_the_workspace(self):
		# Channels populate their workspace rather than carving it up, so nothing
		# intersects a channel's own rules — not even a workspace read, which is why
		# expected_channel_members never touches the parent mapping at all.
		channel = frappe._dict(member_rules_json=_tree(_rule("always-ab")))
		with patch("raven_integration.engine.frappe.get_doc", return_value=channel) as gd:
			result = engine.expected_channel_members("RCM-Channel 1")
		self.assertEqual(result, {"a@example.com", "b@example.com"})
		self.assertEqual(gd.call_count, 1, "only the channel mapping is read")


class TestPauseInvariant(FrappeTestCase):
	def test_a_leaf_under_only_or_may_be_paused(self):
		tree = _tree(_rule("always-a"), _rule("always-ab"), joiner="or")
		self.assertTrue(engine.pausable(tree, [0]))

	def test_a_leaf_under_and_may_not(self):
		# Under `and` the rule narrows its group, so dropping it would ADD people —
		# the opposite of what pausing promises.
		tree = _tree(_rule("always-a"), _rule("always-ab"), joiner="and")
		self.assertFalse(engine.pausable(tree, [0]))

	def test_an_or_leaf_inside_an_and_group_may_not(self):
		# The gate is the whole ancestry, not the leaf's own group.
		tree = _tree(
			_rule("always-ab"),
			_tree(_rule("always-a"), _rule("always-b"), joiner="or"),
			joiner="and",
		)
		self.assertFalse(engine.pausable(tree, [1, 0]))

	def test_a_missing_path_is_not_pausable(self):
		self.assertFalse(engine.pausable(_tree(_rule("always-a")), [7]))

	def test_a_group_is_not_pausable(self):
		tree = _tree(_tree(_rule("always-a")))
		self.assertFalse(engine.pausable(tree, [0]))

	def test_a_leaf_under_a_mixed_group_may_not(self):
		# always-ab or always-a and always-a — the group joins with both or and
		# and, not uniformly either. The and alone is enough to block pausing for
		# every leaf in the group, not just the ones next to it.
		tree = {
			"conjunctions": ["or", "and"],
			"conditions": [_rule("always-ab"), _rule("always-a"), _rule("always-a")],
		}
		self.assertFalse(engine.pausable(tree, [0]))
		self.assertFalse(engine.pausable(tree, [1]))
		self.assertFalse(engine.pausable(tree, [2]))
