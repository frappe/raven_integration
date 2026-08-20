from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import engine, registry

_FAKE = ["raven_integration.tests.fake_provider.get_provider"]
_EDGE = "raven_integration.tests.test_engine.get_edge_provider"

# The two answers the FAKE provider has no rule type for: a rule that evaluates
# honestly to nobody, and one whose evaluate() does not return a population at all.
_EDGE_RETURNS = {"nobody": set(), "not-a-population": None}


def get_edge_provider():
	return {
		"name": "EDGE",
		"label": "Edge",
		"rule_types": [{"type": t, "label": t, "fields": []} for t in _EDGE_RETURNS],
		"evaluate": lambda rule_type, config: _EDGE_RETURNS[rule_type],
	}


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
		# (always-ab and paused) or always-a. The paused rule leaves the and-run it
		# sits in; the run is still there, and the or after it still joins what is
		# left rather than sliding onto the survivors.
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
		# The paused rule is an or-run of its own between the two and-runs, so it
		# empties out and the two runs on either side are unioned. Precedence answers
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

	def test_a_skipped_rule_does_not_make_its_siblings_narrow_each_other(self):
		# always-ab or (broken and always-a), with the provider unable to evaluate the
		# middle rule on the lenient path. Skipping a rule may only ever leave the
		# population as it was or larger — here the and-run it sat in shrinks to
		# always-a, and the or still unions that with always-ab.
		tree = {
			"conjunctions": ["or", "and"],
			"conditions": [_rule("always-ab"), _rule("no-such-rule-type"), _rule("always-a")],
		}
		with patch("raven_integration.engine.frappe.log_error"):
			self.assertEqual(engine.evaluate_rules(tree, strict=False), {"a@example.com", "b@example.com"})

	def test_a_skipped_first_rule_leaves_its_or_intact(self):
		# (broken and always-a) or always-ab — the mirror image, where the dropped
		# child starts the group instead of sitting between two survivors.
		tree = {
			"conjunctions": ["and", "or"],
			"conditions": [_rule("no-such-rule-type"), _rule("always-a"), _rule("always-ab")],
		}
		with patch("raven_integration.engine.frappe.log_error"):
			self.assertEqual(engine.evaluate_rules(tree, strict=False), {"a@example.com", "b@example.com"})

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


def _edge_rule(rule_type, status="Active"):
	return {"provider": "EDGE", "rule_type": rule_type, "config": {}, "status": status}


class TestUnevaluableIsNotNobody(FrappeTestCase):
	"""A tree nothing could evaluate must not read as a tree that matches nobody.

	The two answers are opposite instructions to a caller diffing membership:
	"matches nobody" removes every rule-managed member, "could not be evaluated"
	removes none of them and is what the strict sync path raises over.
	"""

	def setUp(self):
		self._patch = patch.object(registry, "_provider_paths", lambda: [*_FAKE, _EDGE])
		self._patch.start()
		self.addCleanup(self._patch.stop)
		self._log = patch("raven_integration.engine.frappe.log_error")
		self._log.start()
		self.addCleanup(self._log.stop)
		frappe.clear_messages()
		self.addCleanup(frappe.clear_messages)

	def test_a_tree_no_provider_could_evaluate_is_unknown(self):
		self.assertIsNone(engine.evaluate_rules_or_unknown(_tree(_rule("no-such-rule-type"))))

	def test_a_tree_that_honestly_matches_nobody_is_an_empty_set(self):
		self.assertEqual(engine.evaluate_rules_or_unknown(_tree(_edge_rule("nobody"))), set())

	def test_a_tree_of_only_paused_rules_is_unknown(self):
		self.assertIsNone(engine.evaluate_rules_or_unknown(_tree(_rule("always-a", status="Paused"))))

	def test_no_tree_at_all_is_unknown(self):
		self.assertIsNone(engine.evaluate_rules_or_unknown(None))
		self.assertIsNone(engine.evaluate_rules_or_unknown(_tree()))

	def test_what_could_be_evaluated_is_still_answered(self):
		tree = _tree(_rule("no-such-rule-type"), _rule("always-a"), joiner="or")
		self.assertEqual(engine.evaluate_rules_or_unknown(tree), {"a@example.com"})

	def test_it_is_lenient_by_default_and_strict_on_request(self):
		tree = _tree(_rule("no-such-rule-type"))
		self.assertIsNone(engine.evaluate_rules_or_unknown(tree))
		with self.assertRaises(frappe.ValidationError):
			engine.evaluate_rules_or_unknown(tree, strict=True)

	def test_evaluate_rules_still_flattens_unknown_to_nobody(self):
		# Its callers act on the answer, and they have always wanted an empty set for
		# a tree that says nothing. Changing that would change every one of them.
		self.assertEqual(engine.evaluate_rules(_tree(_rule("no-such-rule-type")), strict=False), set())
		self.assertEqual(engine.evaluate_rules(None), set())
		self.assertEqual(engine.evaluate_rules(_tree(_rule("always-a"))), {"a@example.com"})


class TestSwallowedRuleErrorsStayQuiet(FrappeTestCase):
	def setUp(self):
		self._patch = patch.object(registry, "_provider_paths", lambda: [*_FAKE, _EDGE])
		self._patch.start()
		self.addCleanup(self._patch.stop)
		frappe.clear_messages()
		self.addCleanup(frappe.clear_messages)

	def test_a_skipped_rule_leaves_no_error_dialog_queued(self):
		# frappe.throw queues its message before raising, so swallowing the exception
		# without clearing it returns 200 carrying _server_messages and the settings
		# page pops a red dialog on a request that succeeded.
		tree = _tree(_rule("no-such-rule-type"), _rule("always-a"))
		with patch("raven_integration.engine.frappe.log_error"):
			engine.evaluate_rules(tree, strict=False)
		self.assertEqual(list(frappe.message_log), [])

	def test_a_strict_evaluation_still_shows_the_user_the_error(self):
		tree = _tree(_rule("no-such-rule-type"))
		with self.assertRaises(frappe.ValidationError):
			engine.evaluate_rules(tree)
		self.assertEqual(len(frappe.message_log), 1)

	def test_one_unevaluable_provider_logs_once_not_once_per_rule(self):
		# A tree may hold MAX_TREE_NODES leaves and this runs on a read endpoint, so
		# one Error Log document per leaf per reload is the amplification being fixed.
		tree = _tree(*[_rule("no-such-rule-type") for _ in range(5)])
		with patch("raven_integration.engine.frappe.log_error") as log:
			engine.evaluate_rules(tree, strict=False)
		self.assertEqual(log.call_count, 1)

	def test_the_one_log_still_names_the_provider_the_rule_type_and_the_paths(self):
		tree = _tree(_rule("no-such-rule-type"), _rule("no-such-rule-type"))
		with patch("raven_integration.engine.frappe.log_error") as log:
			engine.evaluate_rules(tree, strict=False)
		message = log.call_args.kwargs["message"]
		self.assertIn("FAKE", message)
		self.assertIn("no-such-rule-type", message)
		self.assertIn("[0]", message)
		self.assertIn("[1]", message)

	def test_two_different_failures_are_both_reported_in_the_one_log(self):
		# Deduplication is by what actually differs, so collapsing to one entry must
		# not lose the second provider's problem.
		tree = _tree(_rule("no-such-rule-type"), _edge_rule("not-a-population"))
		with patch("raven_integration.engine.frappe.log_error") as log:
			engine.evaluate_rules(tree, strict=False)
		self.assertEqual(log.call_count, 1)
		message = log.call_args.kwargs["message"]
		self.assertIn("FAKE", message)
		self.assertIn("EDGE", message)

	def test_a_provider_returning_something_that_is_not_a_population_is_skipped(self):
		# Before the registry validated its return, this was a TypeError from set(None)
		# that escaped the lenient handler and 500ed the channel detail page.
		tree = _tree(_edge_rule("not-a-population"), _rule("always-a"))
		with patch("raven_integration.engine.frappe.log_error"):
			self.assertEqual(engine.evaluate_rules(tree, strict=False), {"a@example.com"})

	def test_skipped_disabled_rules_log_once_and_queue_no_dialog(self):
		tree = _tree(*[_rule("no-such-rule-type", status="Paused") for _ in range(4)])
		with patch("raven_integration.engine.frappe.log_error") as log:
			self.assertEqual(engine.disabled_rule_members(tree, strict=False), set())
		self.assertEqual(log.call_count, 1)
		self.assertEqual(list(frappe.message_log), [])

	def test_nothing_is_logged_when_every_rule_evaluated(self):
		with patch("raven_integration.engine.frappe.log_error") as log:
			engine.evaluate_rules(_tree(_rule("always-a")), strict=False)
		log.assert_not_called()
