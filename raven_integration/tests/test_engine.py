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


class TestEngineRuleCombination(FrappeTestCase):
	def setUp(self):
		self._patch = patch.object(registry, "_provider_paths", lambda: _FAKE)
		self._patch.start()
		self.addCleanup(self._patch.stop)

	def test_or_unions_two_rule_populations(self):
		rules = [_rule("always-a"), _rule("always-ab")]
		result = engine.evaluate_rules(rules, combinator=engine.RULE_COMBINATOR_OR)
		self.assertEqual(result, {"a@example.com", "b@example.com"})

	def test_and_intersects_two_rule_populations(self):
		rules = [_rule("always-a"), _rule("always-ab")]
		result = engine.evaluate_rules(rules, combinator=engine.RULE_COMBINATOR_AND)
		self.assertEqual(result, {"a@example.com"})

	def test_paused_rules_are_ignored(self):
		rules = [_rule("always-ab"), _rule("always-a", status="Paused")]
		result = engine.evaluate_rules(rules, combinator=engine.RULE_COMBINATOR_AND)
		# The paused always-a rule does not narrow the result.
		self.assertEqual(result, {"a@example.com", "b@example.com"})

	def test_no_active_rules_means_nobody(self):
		rules = [_rule("always-a", status="Paused")]
		self.assertEqual(engine.evaluate_rules(rules), set())

	def test_channel_members_are_not_narrowed_by_the_workspace(self):
		# Channels populate their workspace rather than carving it up, so nothing
		# intersects a channel's own rules — not even a workspace read, which is why
		# expected_channel_members never touches the parent mapping at all.
		channel = frappe._dict(member_rules=[_rule("always-ab")], rule_combinator=engine.RULE_COMBINATOR_OR)
		with patch("raven_integration.engine.frappe.get_doc", return_value=channel) as gd:
			result = engine.expected_channel_members("RCM-Channel 1")
		self.assertEqual(result, {"a@example.com", "b@example.com"})
		self.assertEqual(gd.call_count, 1, "only the channel mapping is read")
