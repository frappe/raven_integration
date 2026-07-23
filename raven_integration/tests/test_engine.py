from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import engine, registry

_FAKE = ["raven_integration.tests.fake_provider.get_provider"]


class TestWorkspacePopulationCache(FrappeTestCase):
	def test_population_memoized_per_cache(self):
		# With a shared cache, a workspace's rules are evaluated once even when
		# many channels intersect it during one sweep.
		fake_ws = frappe._dict(member_rules=[], rule_combinator="Any (OR)")
		cache: dict = {}
		with patch("raven_integration.engine.frappe.get_doc", return_value=fake_ws) as gd:
			r1 = engine.expected_workspace_members("WS-X", cache=cache)
			r2 = engine.expected_workspace_members("WS-X", cache=cache)
		self.assertEqual(gd.call_count, 1, "second lookup must be served from the cache")
		self.assertEqual(r1, r2)

	def test_no_cache_reevaluates(self):
		fake_ws = frappe._dict(member_rules=[], rule_combinator="Any (OR)")
		with patch("raven_integration.engine.frappe.get_doc", return_value=fake_ws) as gd:
			engine.expected_workspace_members("WS-X")
			engine.expected_workspace_members("WS-X")
		self.assertEqual(gd.call_count, 2, "without a cache each call re-evaluates")


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
