import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import registry
from raven_integration.exceptions import RavenAPIError
from raven_integration.sync_service import (
	_insert_unique_raven_doc,
	add_channel_member,
	add_workspace_member,
	create_raven_workspace_for,
	ensure_raven_user,
	remove_channel_member,
	remove_workspace_member,
	sync_channel_members,
	sync_workspace_members,
)


class TestRavenUserAutoProvision(FrappeTestCase):
	_EMAIL = "raven-prov@example.com"

	def setUp(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		# Purge any leftover from a previous run before inserting.
		if frappe.db.exists("Raven User", {"user": self._EMAIL}):
			frappe.db.delete("Raven User", {"user": self._EMAIL})
		if frappe.db.exists("User", self._EMAIL):
			frappe.db.delete("User", {"name": self._EMAIL})
		self.user = frappe.get_doc(
			{
				"doctype": "User",
				"email": self._EMAIL,
				"first_name": "Prov",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(self._cleanup_provision_fixtures)

	def _cleanup_provision_fixtures(self):
		# Raven User name == user email (autoname: self.name = self.user)
		frappe.db.delete("Raven User", {"user": self._EMAIL})
		frappe.db.delete("User", {"name": self._EMAIL})

	def test_creates_raven_user_if_missing(self):
		ensure_raven_user(self.user.name)
		self.assertTrue(frappe.db.exists("Raven User", {"user": self.user.name}))

	def test_idempotent_on_existing(self):
		ensure_raven_user(self.user.name)
		ensure_raven_user(self.user.name)  # second call must not raise
		count = frappe.db.count("Raven User", {"user": self.user.name})
		self.assertEqual(count, 1)

	def test_skips_disabled_user_silently(self):
		self.user.enabled = 0
		self.user.save()
		ensure_raven_user(self.user.name)  # no raise; just skips
		self.assertFalse(frappe.db.exists("Raven User", {"user": self.user.name}))


class TestEnsureRavenUserConcurrency(unittest.TestCase):
	"""Fully-mocked: no DB, no Raven needed."""

	def test_concurrent_duplicate_insert_is_benign(self):
		# exists() can return False yet insert() trips the unique constraint when a
		# parallel sweep provisioned the same user first. That must be swallowed,
		# not raised and not logged as an error.
		with (
			patch("raven_integration.sync_service.raven_installed", return_value=True),
			patch("raven_integration.sync_service.frappe.db.exists", return_value=False),
			patch("raven_integration.sync_service.frappe.get_doc") as gd,
			patch("raven_integration.sync_service.frappe.log_error") as log,
		):
			doc = gd.return_value
			doc.enabled = 1
			doc.insert.side_effect = frappe.DuplicateEntryError("already exists")
			ensure_raven_user("racer@example.com")  # must not raise
		log.assert_not_called()


class _RavenFixtureMixin:
	"""Shared setup/teardown for tests that need a real Raven workspace + channel + user.

	Uses frappe.db.delete (bypasses hooks) for cleanup so Raven's "last admin" and
	"duplicate member" guards do not interfere with teardown.
	Uses a per-run hash suffix so fixtures never collide across test invocations.
	"""

	def _setUp_raven_fixtures(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")

		suffix = frappe.generate_hash(length=6)

		self.user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"raven-member-{suffix}@example.com",
				"first_name": "MemberTest",
				"send_welcome_email": 0,
			}
		).insert()

		self.raven_workspace = frappe.get_doc(
			{
				"doctype": "Raven Workspace",
				"workspace_name": f"RI Test WS {suffix}",
				"type": "Private",
			}
		).insert(ignore_permissions=True)

		self.raven_channel = frappe.get_doc(
			{
				"doctype": "Raven Channel",
				"channel_name": f"ri-test-ch-{suffix}",
				"workspace": self.raven_workspace.name,
				"type": "Private",
			}
		).insert(ignore_permissions=True)

	def _tearDown_raven_fixtures(self):
		# LIFO with frappe.db.delete to bypass Raven's on_trash hooks.
		# Channel members → channel → workspace members → workspace → Raven User → User.
		frappe.db.delete("Raven Channel Member", {"channel_id": self.raven_channel.name})
		frappe.db.delete("Raven Channel", {"name": self.raven_channel.name})
		frappe.db.delete("Raven Workspace Member", {"workspace": self.raven_workspace.name})
		frappe.db.delete("Raven Workspace", {"name": self.raven_workspace.name})

		raven_user = frappe.db.exists("Raven User", {"user": self.user.name})
		if raven_user:
			frappe.db.delete("Raven User", {"name": raven_user})

		frappe.db.delete("User", {"name": self.user.name})


class TestAddChannelMember(_RavenFixtureMixin, FrappeTestCase):
	def setUp(self):
		self._setUp_raven_fixtures()

	def tearDown(self):
		self._tearDown_raven_fixtures()

	def test_adds_member_with_rule_flag(self):
		add_channel_member(self.raven_channel.name, self.user.name)
		name = frappe.db.exists(
			"Raven Channel Member",
			{"channel_id": self.raven_channel.name, "user_id": self.user.name},
		)
		self.assertIsNotNone(name)
		self.assertEqual(frappe.db.get_value("Raven Channel Member", name, "added_by_rule"), 1)

	def test_add_also_adds_workspace_member(self):
		add_channel_member(self.raven_channel.name, self.user.name)
		self.assertTrue(
			frappe.db.exists(
				"Raven Workspace Member",
				{"workspace": self.raven_workspace.name, "user": self.user.name},
			)
		)

	def test_remove_deletes_rule_managed_row(self):
		add_channel_member(self.raven_channel.name, self.user.name)
		remove_channel_member(self.raven_channel.name, self.user.name)
		self.assertFalse(
			frappe.db.exists(
				"Raven Channel Member",
				{"channel_id": self.raven_channel.name, "user_id": self.user.name},
			)
		)

	def test_remove_does_not_delete_manually_added_row(self):
		# Ensure Raven User exists so link validation passes.
		ensure_raven_user(self.user.name)
		# Insert a row WITHOUT the rule flag (simulating a manual Raven add).
		frappe.get_doc(
			{
				"doctype": "Raven Channel Member",
				"channel_id": self.raven_channel.name,
				"user_id": self.user.name,
				"added_by_rule": 0,
			}
		).insert(ignore_permissions=True)
		remove_channel_member(self.raven_channel.name, self.user.name)
		# Row must still exist since added_by_rule=0.
		self.assertTrue(
			frappe.db.exists(
				"Raven Channel Member",
				{"channel_id": self.raven_channel.name, "user_id": self.user.name},
			)
		)


class TestConcurrentAdd(_RavenFixtureMixin, FrappeTestCase):
	def setUp(self):
		self._setUp_raven_fixtures()

	def tearDown(self):
		self._tearDown_raven_fixtures()

	def test_two_back_to_back_adds_create_one_row(self):
		"""UniqueValidationError on the second insert must be caught, not raised."""
		add_channel_member(self.raven_channel.name, self.user.name)
		add_channel_member(self.raven_channel.name, self.user.name)  # no-op
		count = frappe.db.count(
			"Raven Channel Member",
			{"channel_id": self.raven_channel.name, "user_id": self.user.name},
		)
		self.assertEqual(count, 1)

	def test_two_back_to_back_workspace_adds_create_one_row(self):
		add_workspace_member(self.raven_workspace.name, self.user.name)
		add_workspace_member(self.raven_workspace.name, self.user.name)  # no-op
		count = frappe.db.count(
			"Raven Workspace Member",
			{"workspace": self.raven_workspace.name, "user": self.user.name},
		)
		self.assertEqual(count, 1)


class _SyncedChannelMixin(_RavenFixtureMixin):
	"""A channel mapping carrying one active rule, over the fixture Raven records.

	The rule's population is irrelevant (these tests mock the engine) but its
	presence is what makes the sync authoritative — a rule-less mapping is correctly
	a no-op.
	"""

	def _setUp_synced_channel(self):
		self._setUp_raven_fixtures()

		# The fake provider must be resolvable while the fixture's rules are saved
		# (Raven Channel Mapping.validate calls validate_member_rules, which resolves
		# every leaf's provider through the registry).
		self._reg_patch = patch.object(
			registry, "_provider_paths", lambda: ["raven_integration.tests.fake_provider.get_provider"]
		)
		self._reg_patch.start()
		self.addCleanup(self._reg_patch.stop)

		self.suffix = frappe.generate_hash(length=6)
		suffix = self.suffix

		# Raven Workspace Mapping — skip_raven_create; point at fixture workspace.
		self.ws_map = frappe.new_doc("Raven Workspace Mapping")
		self.ws_map.workspace_label = f"Sync Test WS {suffix}"
		self.ws_map.workspace_type = "Private"
		self.ws_map.flags.skip_raven_create = True
		self.ws_map.insert()
		frappe.db.set_value(
			"Raven Workspace Mapping", self.ws_map.name, "raven_workspace", self.raven_workspace.name
		)

		# Raven Channel Mapping — skip_raven_create; point at fixture channel.
		self.ch_map = frappe.new_doc("Raven Channel Mapping")
		self.ch_map.channel_label = f"Sync Test Ch {suffix}"
		self.ch_map.workspace = self.ws_map.name
		self.ch_map.channel_type = "Private"
		self.ch_map.flags.skip_raven_create = True
		self.ch_map.member_rules_json = frappe.as_json(
			{
				"conjunctions": [],
				"conditions": [
					{
						"label": "Always A",
						"provider": "FAKE",
						"rule_type": "always-a",
						"config": {},
						"status": "Active",
					}
				],
			}
		)
		self.ch_map.insert()
		frappe.db.set_value(
			"Raven Channel Mapping", self.ch_map.name, "raven_channel", self.raven_channel.name
		)

	def _tearDown_synced_channel(self):
		if frappe.db.exists("Raven Channel Mapping", self.ch_map.name):
			frappe.delete_doc("Raven Channel Mapping", self.ch_map.name, force=True, ignore_permissions=True)
		if frappe.db.exists("Raven Workspace Mapping", self.ws_map.name):
			frappe.delete_doc(
				"Raven Workspace Mapping", self.ws_map.name, force=True, ignore_permissions=True
			)
		self._tearDown_raven_fixtures()

	def _channel_member_exists(self, user: str, rule_flag: int | None = None) -> bool:
		filters = {"channel_id": self.raven_channel.name, "user_id": user}
		if rule_flag is not None:
			filters["added_by_rule"] = rule_flag
		return bool(frappe.db.exists("Raven Channel Member", filters))

	def _member_user(self, tag: str, *, enabled: bool = True) -> str:
		"""Another user for the fixture channel. Disabled ones get no Raven User.

		A user with no roles is a Website User, so Raven's own auto_add_system_users
		never provisions them either — which is what leaves the reqd user link with
		nothing to resolve.
		"""
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"ri-sync-{tag}-{self.suffix}@example.com",
				"first_name": f"Sync {tag}",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(lambda: frappe.db.delete("User", {"name": user.name}))
		self.addCleanup(lambda: frappe.db.delete("Raven User", {"user": user.name}))
		if not enabled:
			user.enabled = 0
			user.save()
		return user.name

	def _rule_managed_row(self, user: str) -> None:
		"""A channel member row this app's rules could have written."""
		ensure_raven_user(user)
		frappe.get_doc(
			{
				"doctype": "Raven Channel Member",
				"channel_id": self.raven_channel.name,
				"user_id": user,
				"added_by_rule": 1,
			}
		).insert(ignore_permissions=True)


class TestSyncChannelMembers(_SyncedChannelMixin, FrappeTestCase):
	"""sync_channel_members diff-apply + manual-add protection (provider-agnostic)."""

	def setUp(self):
		self._setUp_synced_channel()

	def tearDown(self):
		self._tearDown_synced_channel()

	def test_adds_missing_members(self):
		"""sync_channel_members adds users returned by the engine with added_by_rule=1."""
		with patch("raven_integration.engine.expected_channel_members", return_value={self.user.name}):
			result = sync_channel_members(self.ch_map.name)
		self.assertNotIn("skipped", result)
		self.assertEqual(result["added"], 1)
		self.assertTrue(self._channel_member_exists(self.user.name, rule_flag=1))

	def test_removes_rule_managed_members_no_longer_matching(self):
		"""Users added by rule who no longer match are removed."""
		with patch("raven_integration.engine.expected_channel_members", return_value={self.user.name}):
			sync_channel_members(self.ch_map.name)
		self.assertTrue(self._channel_member_exists(self.user.name, rule_flag=1))

		with patch("raven_integration.engine.expected_channel_members", return_value=set()):
			result = sync_channel_members(self.ch_map.name)
		self.assertNotIn("skipped", result)
		self.assertEqual(result["removed"], 1)
		self.assertFalse(self._channel_member_exists(self.user.name))

	def test_does_not_remove_manually_added_members(self):
		"""Users added directly in Raven (added_by_rule=0) are never removed by sync."""
		ensure_raven_user(self.user.name)
		frappe.get_doc(
			{
				"doctype": "Raven Channel Member",
				"channel_id": self.raven_channel.name,
				"user_id": self.user.name,
				"added_by_rule": 0,
			}
		).insert(ignore_permissions=True)

		with patch("raven_integration.engine.expected_channel_members", return_value=set()):
			sync_channel_members(self.ch_map.name)

		# The manually-added row must still exist.
		self.assertTrue(self._channel_member_exists(self.user.name))

	def test_is_idempotent(self):
		"""Two back-to-back syncs produce identical state (no extra adds or removes)."""
		with patch("raven_integration.engine.expected_channel_members", return_value={self.user.name}):
			result1 = sync_channel_members(self.ch_map.name)
			result2 = sync_channel_members(self.ch_map.name)

		self.assertNotIn("skipped", result1)
		self.assertEqual(result2["added"], 0)
		self.assertEqual(result2["removed"], 0)

		count = frappe.db.count(
			"Raven Channel Member",
			{"channel_id": self.raven_channel.name, "user_id": self.user.name, "added_by_rule": 1},
		)
		self.assertEqual(count, 1)


class TestOneBadMemberDoesNotAbortTheDiff(_SyncedChannelMixin, FrappeTestCase):
	"""Regression: one un-insertable user used to abort a channel's whole diff.

	ensure_raven_user leaves a disabled user without a Raven User on purpose, so the
	reqd user_id link throws LinkValidationError. That is a ValidationError, and
	add_channel_member only caught the duplicate pair (UniqueValidationError and
	DuplicateEntryError, which is a NameError), so it escaped the add loop and the
	removal loop below it never ran — silently, on every sweep, for as long as the
	disabled user stayed enrolled.
	"""

	def setUp(self):
		self._setUp_synced_channel()
		self.good = self._member_user("good")
		self.blocked = self._member_user("blocked", enabled=False)
		self.stale = self._member_user("stale")
		self._rule_managed_row(self.stale)

	def tearDown(self):
		self._tearDown_synced_channel()

	def test_the_rest_of_the_sweep_still_runs(self):
		with patch(
			"raven_integration.engine.expected_channel_members", return_value={self.good, self.blocked}
		):
			result = sync_channel_members(self.ch_map.name)

		self.assertTrue(self._channel_member_exists(self.good, rule_flag=1))
		self.assertFalse(
			self._channel_member_exists(self.stale),
			"the removal pass runs whatever the add pass could not write",
		)
		self.assertEqual(result, {"added": 1, "removed": 1})

	def test_the_member_it_could_not_write_is_logged(self):
		with (
			patch("raven_integration.engine.expected_channel_members", return_value={self.blocked}),
			patch("raven_integration.sync_service.frappe.log_error") as log,
		):
			sync_channel_members(self.ch_map.name)

		titles = [call.kwargs.get("title", "") for call in log.call_args_list]
		self.assertTrue(any(self.blocked in title for title in titles), titles)

	def test_the_member_it_could_not_write_is_not_counted(self):
		with patch("raven_integration.engine.expected_channel_members", return_value={self.blocked}):
			result = sync_channel_members(self.ch_map.name)

		self.assertEqual(result["added"], 0, "the count is rows written, not members proposed")
		self.assertFalse(self._channel_member_exists(self.blocked))

	def test_a_half_written_member_is_rolled_back(self):
		"""The savepoint, not a bare except: what the failed add wrote has to go too.

		add_channel_member joins the parent workspace before the channel, so a member
		whose channel row throws has already written a workspace row by then.
		"""

		def add_then_fail(channel, user):
			add_workspace_member(self.raven_workspace.name, user)
			frappe.throw("Could not find Raven User", frappe.LinkValidationError)

		with (
			patch("raven_integration.engine.expected_channel_members", return_value={self.good}),
			patch("raven_integration.sync_service.add_channel_member", add_then_fail),
		):
			result = sync_channel_members(self.ch_map.name)

		self.assertFalse(
			frappe.db.exists(
				"Raven Workspace Member", {"workspace": self.raven_workspace.name, "user": self.good}
			)
		)
		self.assertEqual(result, {"added": 0, "removed": 1})


class TestErrorDistinction(unittest.TestCase):
	"""create_raven_workspace_for wraps inner exceptions as RavenAPIError
	and preserves the original exception class name in the message."""

	def test_permission_error_raises_raven_api_error_with_class_name(self):
		"""frappe.PermissionError during insert must surface as RavenAPIError whose
		message contains 'PermissionError'."""
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")

		ws_map = frappe._dict(
			workspace_label="Error Test WS",
			workspace_type="Private",
			raven_workspace=None,
		)

		with patch("frappe.get_doc") as mock_get_doc:
			mock_doc = mock_get_doc.return_value
			mock_doc.insert.side_effect = frappe.PermissionError("You don't have permission")

			with self.assertRaises(RavenAPIError) as ctx:
				create_raven_workspace_for(ws_map)

		self.assertIn("PermissionError", str(ctx.exception))


class TestNoActiveRulesIsNotAuthoritative(unittest.TestCase):
	"""Regression: deleting or pausing a channel's last rule must not evict everyone.

	Before the fix, a rule-less mapping evaluated to an empty population and the
	diff treated that as authoritative, so to_remove became every rule-managed
	member. Reproduced against a real site as {'added': 0, 'removed': 5}.

	The guard is channel-only now. A workspace carries no rules at all, so it has
	nothing to be "no opinion" about — see TestDerivedWorkspaceSync.
	"""

	_MEMBERS = ("a@example.com", "b@example.com", "c@example.com")

	def _sync_channel(self, rules, expected=None):
		channel = frappe._dict(
			member_rules_json={"conjunctions": ["or"] * max(len(rules) - 1, 0), "conditions": list(rules)},
			stale=0,
		)
		values = {"enabled": 1, "raven_channel": "RC-1"}
		expectation = (
			patch("raven_integration.engine.expected_channel_members", return_value=expected)
			if expected is not None
			else patch("raven_integration.engine.frappe.get_doc", return_value=channel)
		)
		with (
			patch("raven_integration.sync_service.raven_installed", return_value=True),
			patch(
				"raven_integration.sync_service.frappe.db.get_value", side_effect=lambda dt, n, f: values[f]
			),
			patch("raven_integration.sync_service.frappe.get_doc", return_value=channel),
			patch("raven_integration.sync_service.frappe.get_all", return_value=list(self._MEMBERS)),
			expectation,
			patch("raven_integration.sync_service._fire_after_synced"),
			patch("raven_integration.sync_service.remove_channel_member") as rm,
		):
			result = sync_channel_members("RCM-1")
		return result, rm

	def test_zero_rules_removes_nobody(self):
		result, rm = self._sync_channel([])
		rm.assert_not_called()
		self.assertEqual(result, {"skipped": True, "reason": "no_active_rules"})

	def test_all_rules_paused_removes_nobody(self):
		paused = [{"provider": "FAKE", "rule_type": "always-a", "config": {}, "status": "Paused"}]
		result, rm = self._sync_channel(paused)
		rm.assert_not_called()
		self.assertEqual(result, {"skipped": True, "reason": "no_active_rules"})

	def test_active_rule_matching_nobody_still_removes(self):
		# The guard must not swallow a genuine empty population: an active rule that
		# legitimately matches nobody is authoritative and evicts.
		active = [{"provider": "FAKE", "rule_type": "always-none", "config": {}, "status": "Active"}]
		result, rm = self._sync_channel(active, expected=set())
		self.assertEqual(rm.call_count, len(self._MEMBERS))
		self.assertEqual(result, {"added": 0, "removed": len(self._MEMBERS)})


class TestDerivedWorkspaceSync(unittest.TestCase):
	"""A workspace has no rules: its membership is whoever is in one of its channels."""

	_MEMBERS = ("a@example.com", "b@example.com", "c@example.com")

	def _sync_workspace(self, derived):
		ws = frappe._dict(member_rules_json=None, stale=0)
		# No `enabled`: a workspace mapping has no such field, and the sync path is
		# not gated on one — a KeyError here would mean it started reading it again.
		values = {"raven_workspace": "RW-1"}
		with (
			patch("raven_integration.sync_service.raven_installed", return_value=True),
			patch(
				"raven_integration.sync_service.frappe.db.get_value", side_effect=lambda dt, n, f: values[f]
			),
			patch("raven_integration.sync_service.frappe.get_doc", return_value=ws),
			patch("raven_integration.sync_service.frappe.get_all", return_value=list(self._MEMBERS)),
			patch("raven_integration.engine.expected_workspace_members", return_value=set(derived)),
			patch("raven_integration.sync_service.add_workspace_member") as add,
			patch("raven_integration.sync_service.remove_workspace_member") as rm,
		):
			result = sync_workspace_members("WSM-1")
		return result, add, rm

	def test_a_ruleless_workspace_is_not_skipped(self):
		# The old "no opinion" guard would have skipped this outright, leaving the
		# derived membership permanently unreconciled.
		result, _add, rm = self._sync_workspace(self._MEMBERS)
		self.assertEqual(result, {"added": 0, "removed": 0})
		rm.assert_not_called()

	def test_losing_the_last_channel_removes_from_the_workspace(self):
		result, _add, rm = self._sync_workspace(["a@example.com", "b@example.com"])
		self.assertEqual(result, {"added": 0, "removed": 1})
		rm.assert_called_once_with("RW-1", "c@example.com")

	def test_a_channel_member_with_no_workspace_row_is_added(self):
		derived = [*self._MEMBERS, "d@example.com"]
		result, add, _rm = self._sync_workspace(derived)
		self.assertEqual(result, {"added": 1, "removed": 0})
		add.assert_called_once_with("RW-1", "d@example.com")


def _raven_supports_silent_add() -> bool:
	"""True if the installed Raven honours flags.ignore_system_message.

	Added in Raven v3; earlier versions post one System message per member and
	silently ignore the flag.
	"""
	import inspect

	try:
		from raven.raven_channel_management.doctype.raven_channel_member.raven_channel_member import (
			RavenChannelMember,
		)
	except ImportError:
		return False
	return "ignore_system_message" in inspect.getsource(RavenChannelMember.after_insert)


class TestSilentChannelAdd(_RavenFixtureMixin, FrappeTestCase):
	"""Rule-driven adds must not post Raven's per-member System message.

	Without suppression a first sync of a 500-student course puts 500 "X added Y."
	messages into that course's channel.
	"""

	def setUp(self):
		self._setUp_raven_fixtures()

	def tearDown(self):
		frappe.db.delete("Raven Message", {"channel_id": self.raven_channel.name})
		self._tearDown_raven_fixtures()

	def _system_messages(self) -> int:
		return frappe.db.count(
			"Raven Message", {"channel_id": self.raven_channel.name, "message_type": "System"}
		)

	def test_add_posts_no_system_message(self):
		if not _raven_supports_silent_add():
			self.skipTest("installed Raven predates flags.ignore_system_message (v3)")
		before = self._system_messages()
		add_channel_member(self.raven_channel.name, self.user.name)
		self.assertEqual(self._system_messages(), before, "rule-driven add must be silent")

	def test_add_creates_the_row_with_rule_flag(self):
		add_channel_member(self.raven_channel.name, self.user.name)
		name = frappe.db.exists(
			"Raven Channel Member",
			{"channel_id": self.raven_channel.name, "user_id": self.user.name},
		)
		self.assertIsNotNone(name)
		self.assertEqual(frappe.db.get_value("Raven Channel Member", name, "added_by_rule"), 1)

	def test_raven_defaults_still_applied(self):
		# Suppressing the message must not cost the row Raven's own defaults.
		add_channel_member(self.raven_channel.name, self.user.name)
		row = frappe.db.get_value(
			"Raven Channel Member",
			{"channel_id": self.raven_channel.name, "user_id": self.user.name},
			["allow_notifications", "last_visit"],
			as_dict=True,
		)
		self.assertEqual(row.allow_notifications, 1)
		self.assertIsNotNone(row.last_visit)

	def test_double_add_still_creates_one_row(self):
		add_channel_member(self.raven_channel.name, self.user.name)
		add_channel_member(self.raven_channel.name, self.user.name)
		count = frappe.db.count(
			"Raven Channel Member",
			{"channel_id": self.raven_channel.name, "user_id": self.user.name},
		)
		self.assertEqual(count, 1)


class TestChannelNameCollision(_RavenFixtureMixin, FrappeTestCase):
	"""Two channels created from the same label must both insert.

	Raven slugifies Raven Channel.channel_name in before_validate, so probing the
	raw label never matches a stored name: the same colliding name was proposed on
	all 5 attempts and Raven's per-workspace duplicate check threw. It threw a bare
	ValidationError, which the retry loop did not even catch.
	"""

	def setUp(self):
		self._setUp_raven_fixtures()
		self._created = []

	def tearDown(self):
		for name in self._created:
			frappe.db.delete("Raven Channel Member", {"channel_id": name})
			frappe.db.delete("Raven Channel", {"name": name})
		self._tearDown_raven_fixtures()

	def _make(self, label):
		doc = _insert_unique_raven_doc(
			{"doctype": "Raven Channel", "workspace": self.raven_workspace.name, "type": "Private"},
			"channel_name",
			label,
		)
		self._created.append(doc.name)
		return doc

	def test_same_label_twice_gets_suffixed(self):
		label = f"Collide {frappe.generate_hash(length=5)}"
		first = self._make(label)
		second = self._make(label)
		self.assertNotEqual(first.name, second.name)
		# Raven slugified both; the second must have been bumped, not rejected.
		self.assertNotEqual(first.channel_name, second.channel_name)

	def test_probe_alone_would_not_have_caught_it(self):
		# Guards the reasoning: the raw label genuinely does not match what Raven
		# stores, so the upfront exists() probe cannot be the only defence.
		label = f"Probe Miss {frappe.generate_hash(length=5)}"
		created = self._make(label)
		self.assertNotEqual(created.channel_name, label)
		self.assertFalse(frappe.db.exists("Raven Channel", {"channel_name": label}))


class _ManagedPairMixin:
	"""Real Raven records behind real mappings, plus users to put in them.

	Everything is created the way the app creates it — the mapping inserts make
	Raven build the workspace, the channel and the owner's admin rows — so the
	hooks Raven fires on the way out are the ones a site would fire.
	"""

	def _setUp_managed(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")

		self.suffix = frappe.generate_hash(length=6)
		self.addCleanup(self._tearDown_managed)

		self.ws_map = frappe.new_doc("Raven Workspace Mapping")
		self.ws_map.workspace_label = f"Evict WS {self.suffix}"
		self.ws_map.workspace_type = "Private"
		self.ws_map.insert()
		self.raven_workspace = self.ws_map.raven_workspace

		self.ch_map = frappe.new_doc("Raven Channel Mapping")
		self.ch_map.channel_label = f"evict-ch-{self.suffix}"
		self.ch_map.workspace = self.ws_map.name
		self.ch_map.channel_type = "Private"
		self.ch_map.insert()
		self.raven_channel = self.ch_map.raven_channel

	def _tearDown_managed(self):
		like = ("like", f"%{self.suffix}%")
		frappe.db.delete("Raven Channel Mapping", {"name": like})
		frappe.db.delete("Raven Workspace Mapping", {"name": like})
		for channel in frappe.get_all("Raven Channel", filters={"name": like}, pluck="name"):
			frappe.db.delete("Raven Channel Member", {"channel_id": channel})
			frappe.db.delete("Raven Message", {"channel_id": channel})
		frappe.db.delete("Raven Channel", {"name": like})
		for workspace in frappe.get_all("Raven Workspace", filters={"name": like}, pluck="name"):
			frappe.db.delete("Raven Workspace Member", {"workspace": workspace})
		frappe.db.delete("Raven Workspace", {"name": like})

	def _unmanaged_channel(self, tag: str) -> str:
		"""A Raven channel in the same workspace that this app never mapped."""
		return (
			frappe.get_doc(
				{
					"doctype": "Raven Channel",
					"channel_name": f"unmanaged-{tag}-{self.suffix}",
					"workspace": self.raven_workspace,
					"type": "Private",
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _user(self, tag: str) -> str:
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"ri-{tag}-{self.suffix}@example.com",
				"first_name": f"Evict {tag}",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(lambda: frappe.db.delete("User", {"name": user.name}))
		ensure_raven_user(user.name)
		self.addCleanup(lambda: frappe.db.delete("Raven User", {"user": user.name}))
		return user.name

	def _channel_row(self, user: str, *, by_rule: bool, channel: str | None = None) -> str:
		return (
			frappe.get_doc(
				{
					"doctype": "Raven Channel Member",
					"channel_id": channel or self.raven_channel,
					"user_id": user,
					"added_by_rule": 1 if by_rule else 0,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _workspace_row(self, user: str, *, by_rule: bool) -> str:
		return (
			frappe.get_doc(
				{
					"doctype": "Raven Workspace Member",
					"workspace": self.raven_workspace,
					"user": user,
					"added_by_rule": 1 if by_rule else 0,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _has_channel_row(self, user: str, channel: str | None = None) -> bool:
		return bool(
			frappe.db.exists(
				"Raven Channel Member", {"channel_id": channel or self.raven_channel, "user_id": user}
			)
		)

	def _has_workspace_row(self, user: str) -> bool:
		return bool(
			frappe.db.exists("Raven Workspace Member", {"workspace": self.raven_workspace, "user": user})
		)


class TestWithdrawalLeavesHumanChannelRowsAlone(_ManagedPairMixin, FrappeTestCase):
	"""Deleting a workspace mapping must not reach into channels it never managed.

	Raven Workspace Member.on_trash runs delete_channel_members_for_user, which
	deletes every channel row the user holds anywhere in the workspace with no
	regard for who put it there. So withdrawing a workspace row is only safe once
	the user has no channel rows left to lose.
	"""

	def setUp(self):
		self._setUp_managed()

	def test_a_hand_added_row_in_an_unmanaged_channel_survives(self):
		bob = self._user("cascade")
		other = self._unmanaged_channel("keep")
		self._channel_row(bob, by_rule=True)
		self._channel_row(bob, by_rule=False, channel=other)
		self._workspace_row(bob, by_rule=True)

		frappe.delete_doc("Raven Workspace Mapping", self.ws_map.name)

		self.assertTrue(
			self._has_channel_row(bob, other),
			"a channel row a human added is not this app's to remove, at any remove",
		)

	def test_a_member_a_channel_still_holds_keeps_the_workspace_row(self):
		"""Workspace membership is derived: one channel left is enough to stay."""
		bob = self._user("derived")
		other = self._unmanaged_channel("derived")
		self._channel_row(bob, by_rule=True)
		self._channel_row(bob, by_rule=False, channel=other)
		self._workspace_row(bob, by_rule=True)

		frappe.delete_doc("Raven Workspace Mapping", self.ws_map.name)

		self.assertTrue(self._has_workspace_row(bob))

	def test_the_rows_its_rules_granted_still_go(self):
		"""The contrast: nothing above weakens the withdrawal itself."""
		bob = self._user("withdrawn")
		self._channel_row(bob, by_rule=True)
		self._workspace_row(bob, by_rule=True)

		frappe.delete_doc("Raven Workspace Mapping", self.ws_map.name)

		self.assertFalse(self._has_channel_row(bob))
		self.assertFalse(self._has_workspace_row(bob))


class TestWithdrawalCannotOrphanTheWorkspaceAdmin(_ManagedPairMixin, FrappeTestCase):
	"""Raven refuses to delete a workspace member row that leaves no admin behind.

	It throws from on_trash, which takes down the whole delete the user actually
	asked for — the mapping, its channel mappings and its rules — with an error
	that names none of them.
	"""

	def setUp(self):
		self._setUp_managed()

	def _make_sole_admin(self, user: str) -> None:
		"""Hand the app's own row the workspace's only admin flag.

		A human promoting a rule-added member in Raven does exactly this; going
		through db.set_value keeps Raven's own validate out of the fixture.
		"""
		frappe.db.set_value(
			"Raven Workspace Member", {"workspace": self.raven_workspace, "user": user}, "is_admin", 1
		)
		for row in frappe.get_all(
			"Raven Workspace Member",
			filters={"workspace": self.raven_workspace, "user": ("!=", user)},
			pluck="name",
		):
			frappe.db.set_value("Raven Workspace Member", row, "is_admin", 0)

	def test_the_mapping_delete_survives_a_rule_managed_last_admin(self):
		admin = self._user("last-admin")
		self._channel_row(admin, by_rule=True)
		self._workspace_row(admin, by_rule=True)
		self._make_sole_admin(admin)

		frappe.delete_doc("Raven Workspace Mapping", self.ws_map.name)

		self.assertFalse(frappe.db.exists("Raven Workspace Mapping", self.ws_map.name))

	def test_the_last_admin_keeps_the_row_the_withdrawal_could_not_take(self):
		admin = self._user("kept-admin")
		self._channel_row(admin, by_rule=True)
		self._workspace_row(admin, by_rule=True)
		self._make_sole_admin(admin)

		frappe.delete_doc("Raven Workspace Mapping", self.ws_map.name)

		self.assertTrue(
			self._has_workspace_row(admin),
			"Raven keeps its last admin; the withdrawal has to leave that row where it is",
		)


class TestTheWorkspaceSweepClaimsNothingItDidNotAdd(_ManagedPairMixin, FrappeTestCase):
	"""The workspace pass carries no rules: its expected set is every channel member.

	That says who is in a channel, not who this app put there, so a row it finds
	is not evidence of anything it can later withdraw.
	"""

	def setUp(self):
		self._setUp_managed()

	def _added_by_rule(self, user: str):
		return frappe.db.get_value(
			"Raven Workspace Member", {"workspace": self.raven_workspace, "user": user}, "added_by_rule"
		)

	def test_a_hand_added_workspace_row_is_not_claimed(self):
		alice = self._user("hand-claim")
		self._channel_row(alice, by_rule=False)
		self._workspace_row(alice, by_rule=False)

		sync_workspace_members(self.ws_map.name)

		self.assertEqual(self._added_by_rule(alice), 0)

	def test_the_owners_admin_row_is_not_claimed(self):
		"""Raven Workspace.create_member_for_owner wrote this row, not the app."""
		sync_workspace_members(self.ws_map.name)

		self.assertEqual(self._added_by_rule(frappe.session.user), 0)

	def test_a_hand_added_workspace_row_survives_losing_its_channel(self):
		alice = self._user("hand-survive")
		self._channel_row(alice, by_rule=False)
		self._workspace_row(alice, by_rule=False)

		sync_workspace_members(self.ws_map.name)
		# A human takes her out of the channel from inside Raven.
		frappe.db.delete("Raven Channel Member", {"channel_id": self.raven_channel, "user_id": alice})
		sync_workspace_members(self.ws_map.name)

		self.assertTrue(
			self._has_workspace_row(alice),
			"the app never added this row, so no sweep of its own may remove it",
		)

	def test_a_second_sweep_adds_nothing(self):
		"""Rows it will not claim must not be re-added on every sweep either.

		Raven answers a duplicate insert with a thrown ValidationError, which lands
		in the message log of whatever request drove the sweep.
		"""
		alice = self._user("idempotent")
		self._channel_row(alice, by_rule=False)
		self._workspace_row(alice, by_rule=False)

		sync_workspace_members(self.ws_map.name)
		second = sync_workspace_members(self.ws_map.name)

		self.assertEqual(second, {"added": 0, "removed": 0})

	def test_a_rule_added_member_is_still_withdrawn_when_their_last_channel_goes(self):
		"""The contrast: what the app did add, it still takes back."""
		bob = self._user("rule-sweep")
		self._channel_row(bob, by_rule=True)
		self._workspace_row(bob, by_rule=True)

		frappe.db.delete("Raven Channel Member", {"channel_id": self.raven_channel, "user_id": bob})
		result = sync_workspace_members(self.ws_map.name)

		self.assertEqual(result["removed"], 1)
		self.assertFalse(self._has_workspace_row(bob))


class TestWithdrawalLeavesTheChannelOpen(_ManagedPairMixin, FrappeTestCase):
	"""Raven archives a private channel whose last member leaves.

	That reads a human walking out of a conversation. A mapping delete empties the
	channel without anyone leaving it, and the channel is the one thing the delete
	promises to leave standing.
	"""

	def setUp(self):
		self._setUp_managed()
		# The manager who created the channel has since left it, so every row left
		# in it is one the app's rules put there.
		frappe.db.delete("Raven Channel Member", {"channel_id": self.raven_channel})

	def _populate(self, count: int = 2) -> list[str]:
		users = []
		for i in range(count):
			user = self._user(f"open{i}")
			self._channel_row(user, by_rule=True)
			self._workspace_row(user, by_rule=True)
			users.append(user)
		return users

	def _is_archived(self) -> int:
		return frappe.db.get_value("Raven Channel", self.raven_channel, "is_archived")

	def test_deleting_the_channel_mapping_does_not_archive_the_channel(self):
		self._populate()

		frappe.delete_doc("Raven Channel Mapping", self.ch_map.name)

		self.assertEqual(self._is_archived(), 0, "the Raven channel is meant to survive its mapping")

	def test_deleting_the_workspace_mapping_does_not_archive_the_channel(self):
		self._populate()

		frappe.delete_doc("Raven Workspace Mapping", self.ws_map.name)

		self.assertEqual(self._is_archived(), 0)

	def test_a_channel_a_human_had_already_archived_stays_archived(self):
		self._populate()
		frappe.db.set_value("Raven Channel", self.raven_channel, "is_archived", 1)

		frappe.delete_doc("Raven Channel Mapping", self.ch_map.name)

		self.assertEqual(self._is_archived(), 1, "un-archiving is not this app's decision either")

	def test_the_withdrawal_still_empties_the_channel(self):
		"""The contrast: keeping the channel open is not keeping its members."""
		users = self._populate()

		frappe.delete_doc("Raven Channel Mapping", self.ch_map.name)

		for user in users:
			self.assertFalse(self._has_channel_row(user))

	def test_raven_posts_a_removal_message_for_every_withdrawn_member(self):
		"""Pins Raven 2.x's per-member System message on the withdrawal path.

		RavenChannelMember.after_delete posts "X removed Y." for each row and reads
		no suppression flag — `flags.ignore_system_message` appears nowhere in the
		installed Raven, so this app cannot ask for silence the way add_channel_member
		does. A 500-student course therefore ends its mapping with 500 messages in the
		channel. Pins current behaviour: once the installed Raven honours the flag,
		set it on the delete and invert this to assertEqual(after, before).
		"""
		if _raven_supports_silent_add():
			self.skipTest("installed Raven honours flags.ignore_system_message")
		users = self._populate()
		before = frappe.db.count(
			"Raven Message", {"channel_id": self.raven_channel, "message_type": "System"}
		)

		frappe.delete_doc("Raven Channel Mapping", self.ch_map.name)

		self.assertEqual(
			frappe.db.count("Raven Message", {"channel_id": self.raven_channel, "message_type": "System"}),
			before + len(users),
		)
