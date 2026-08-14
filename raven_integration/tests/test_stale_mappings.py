import inspect
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import registry


class _StaleFixtureMixin:
	"""A real Raven Workspace + Channel behind a real mapping pair.

	Teardown deletes by label suffix rather than by remembered name: a recreate
	leaves the original Raven record deleted and a differently-named one in its
	place, and both must go. frappe.db.delete bypasses Raven's own on_trash guards.
	"""

	def _setUp_mappings(self, with_rule: bool = False):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")

		self.suffix = frappe.generate_hash(length=6)
		self.addCleanup(self._tearDown_mappings)

		if with_rule:
			# Rules are validated against their provider on save, so the fake
			# provider has to be resolvable for as long as the fixture lives.
			reg_patch = patch.object(
				registry,
				"_provider_paths",
				lambda: ["raven_integration.tests.fake_provider.get_provider"],
			)
			reg_patch.start()
			self.addCleanup(reg_patch.stop)

		self.ws_map = frappe.new_doc("Raven Workspace Mapping")
		self.ws_map.workspace_label = f"Stale WS {self.suffix}"
		self.ws_map.workspace_type = "Private"
		self.ws_map.insert()

		self.ch_map = frappe.new_doc("Raven Channel Mapping")
		self.ch_map.channel_label = f"stale-ch-{self.suffix}"
		self.ch_map.workspace = self.ws_map.name
		self.ch_map.channel_type = "Private"
		if with_rule:
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

	def _tearDown_mappings(self):
		like = ("like", f"%{self.suffix}%")
		frappe.db.delete("Raven Channel Mapping", {"name": like})
		frappe.db.delete("Raven Workspace Mapping", {"name": like})
		for channel in frappe.get_all("Raven Channel", filters={"name": like}, pluck="name"):
			frappe.db.delete("Raven Channel Member", {"channel_id": channel})
		frappe.db.delete("Raven Channel", {"name": like})
		for workspace in frappe.get_all("Raven Workspace", filters={"name": like}, pluck="name"):
			frappe.db.delete("Raven Workspace Member", {"workspace": workspace})
		frappe.db.delete("Raven Workspace", {"name": like})

	def _delete_raven_channel(self):
		frappe.delete_doc("Raven Channel", self.ch_map.raven_channel, ignore_permissions=True)

	def _delete_raven_workspace(self):
		frappe.delete_doc("Raven Workspace", self.ws_map.raven_workspace, ignore_permissions=True)

	def _stale(self, doctype: str, name: str) -> int:
		return frappe.db.get_value(doctype, name, "stale")


class TestRavenDeleteMarksMappingStale(_StaleFixtureMixin, FrappeTestCase):
	"""Deleting the Raven record must flag the mapping, never delete it."""

	def setUp(self):
		self._setUp_mappings()

	def test_deleting_raven_channel_marks_its_mapping_stale(self):
		self.assertEqual(self._stale("Raven Channel Mapping", self.ch_map.name), 0)
		self._delete_raven_channel()
		self.assertEqual(self._stale("Raven Channel Mapping", self.ch_map.name), 1)

	def test_deleting_raven_workspace_marks_its_mapping_stale(self):
		self.assertEqual(self._stale("Raven Workspace Mapping", self.ws_map.name), 0)
		self._delete_raven_workspace()
		self.assertEqual(self._stale("Raven Workspace Mapping", self.ws_map.name), 1)

	def test_mapping_row_survives_the_raven_delete(self):
		"""The rules are the user's work — a Raven-side delete must not destroy them."""
		self._delete_raven_channel()
		self.assertTrue(frappe.db.exists("Raven Channel Mapping", self.ch_map.name))
		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.ch_map.name, "channel_label"),
			self.ch_map.channel_label,
		)

	def test_deleting_a_channel_does_not_stale_its_workspace_mapping(self):
		self._delete_raven_channel()
		self.assertEqual(self._stale("Raven Workspace Mapping", self.ws_map.name), 0)

	def test_deleting_a_channel_does_not_stale_an_unrelated_mapping(self):
		other = frappe.new_doc("Raven Channel Mapping")
		other.channel_label = f"stale-other-{self.suffix}"
		other.workspace = self.ws_map.name
		other.channel_type = "Private"
		other.insert()

		self._delete_raven_channel()

		self.assertEqual(self._stale("Raven Channel Mapping", other.name), 0)


class TestStaleMappingsAreNotSynced(_StaleFixtureMixin, FrappeTestCase):
	"""A stale mapping points at a record Raven no longer has; syncing it would
	write members into nothing."""

	def setUp(self):
		self._setUp_mappings()

	def test_stale_channel_mapping_is_skipped(self):
		from raven_integration.sync_service import sync_channel_members

		self._delete_raven_channel()
		self.assertEqual(
			sync_channel_members(self.ch_map.name),
			{"skipped": True, "reason": "raven_record_deleted"},
		)

	def test_stale_workspace_mapping_is_skipped(self):
		from raven_integration.sync_service import sync_workspace_members

		self._delete_raven_workspace()
		self.assertEqual(
			sync_workspace_members(self.ws_map.name),
			{"skipped": True, "reason": "raven_record_deleted"},
		)

	def test_staleness_is_checked_before_members_are_evaluated(self):
		"""The guard must short-circuit, not merely discard the result — evaluating
		rules over every user is the expensive half of a sweep."""
		from raven_integration.sync_service import sync_channel_members

		self._delete_raven_channel()
		with patch("raven_integration.engine.expected_channel_members") as expected:
			sync_channel_members(self.ch_map.name)
		expected.assert_not_called()

	def test_a_live_mapping_is_still_synced(self):
		"""Guards the guard: nothing above should make a healthy mapping skip."""
		from raven_integration.sync_service import sync_channel_members

		with patch("raven_integration.engine.expected_channel_members", return_value=set()):
			result = sync_channel_members(self.ch_map.name)
		self.assertEqual(result, {"skipped": True, "reason": "no_active_rules"})


class TestRecreate(_StaleFixtureMixin, FrappeTestCase):
	def setUp(self):
		self._setUp_mappings(with_rule=True)

	def test_recreate_channel_relinks_and_clears_stale(self):
		from raven_integration.api import recreate_channel

		original = self.ch_map.raven_channel
		self._delete_raven_channel()
		self.assertFalse(frappe.db.exists("Raven Channel", original))

		with patch("raven_integration.api.frappe.enqueue"):
			new_channel = recreate_channel(self.ch_map.name)

		# Deleting the old record frees its name, so the recreated channel usually
		# reclaims it — the point is that a live record exists again, not that it
		# is called something new.
		self.assertTrue(frappe.db.exists("Raven Channel", new_channel))
		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.ch_map.name, "raven_channel"),
			new_channel,
		)
		self.assertEqual(self._stale("Raven Channel Mapping", self.ch_map.name), 0)

	def test_recreate_channel_keeps_the_configured_rules(self):
		"""The whole point of recreating rather than deleting: the rules survive."""
		from raven_integration.api import recreate_channel

		self._delete_raven_channel()
		with patch("raven_integration.api.frappe.enqueue"):
			recreate_channel(self.ch_map.name)

		tree = frappe.parse_json(frappe.get_doc("Raven Channel Mapping", self.ch_map.name).member_rules_json)
		self.assertEqual(len(tree["conditions"]), 1)
		self.assertEqual(tree["conditions"][0]["rule_type"], "always-a")
		self.assertEqual(tree["conditions"][0]["status"], "Active")

	def test_recreate_channel_resumes_syncing(self):
		from raven_integration.api import recreate_channel
		from raven_integration.sync_service import sync_channel_members

		self._delete_raven_channel()
		with patch("raven_integration.api.frappe.enqueue"):
			recreate_channel(self.ch_map.name)

		with patch("raven_integration.engine.expected_channel_members", return_value=set()):
			result = sync_channel_members(self.ch_map.name)
		self.assertNotIn("skipped", result)

	def test_recreate_channel_queues_a_resync(self):
		from raven_integration.api import recreate_channel

		self._delete_raven_channel()
		with patch("raven_integration.api.frappe.enqueue") as enqueue:
			recreate_channel(self.ch_map.name)

		enqueue.assert_called_once_with(
			"raven_integration.sync_service.sync_channel_members",
			queue="short",
			enqueue_after_commit=True,
			channel_name=self.ch_map.name,
		)

	def test_recreate_workspace_relinks_and_clears_stale(self):
		from raven_integration.api import recreate_workspace

		original = self.ws_map.raven_workspace
		self._delete_raven_workspace()
		self.assertFalse(frappe.db.exists("Raven Workspace", original))

		with patch("raven_integration.api.frappe.enqueue"):
			new_workspace = recreate_workspace(self.ws_map.name)

		self.assertTrue(frappe.db.exists("Raven Workspace", new_workspace))
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.ws_map.name, "raven_workspace"),
			new_workspace,
		)
		self.assertEqual(self._stale("Raven Workspace Mapping", self.ws_map.name), 0)

	def test_recreate_channel_refuses_while_its_workspace_is_stale(self):
		"""A channel lives inside a workspace; recreating it into a deleted one
		would produce a channel nobody can reach."""
		from raven_integration.api import recreate_channel

		self._delete_raven_channel()
		self._delete_raven_workspace()

		with self.assertRaises(frappe.ValidationError):
			recreate_channel(self.ch_map.name)

		self.assertEqual(self._stale("Raven Channel Mapping", self.ch_map.name), 1)

	def test_recreating_the_workspace_first_unblocks_the_channel(self):
		from raven_integration.api import recreate_channel, recreate_workspace

		self._delete_raven_channel()
		self._delete_raven_workspace()

		with patch("raven_integration.api.frappe.enqueue"):
			new_workspace = recreate_workspace(self.ws_map.name)
			new_channel = recreate_channel(self.ch_map.name)

		self.assertEqual(frappe.db.get_value("Raven Channel", new_channel, "workspace"), new_workspace)
		self.assertEqual(self._stale("Raven Channel Mapping", self.ch_map.name), 0)

	def test_recreate_channel_rejects_a_healthy_mapping(self):
		from raven_integration.api import recreate_channel

		with self.assertRaises(frappe.ValidationError):
			recreate_channel(self.ch_map.name)

		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.ch_map.name, "raven_channel"),
			self.ch_map.raven_channel,
		)

	def test_recreate_workspace_rejects_a_healthy_mapping(self):
		from raven_integration.api import recreate_workspace

		with self.assertRaises(frappe.ValidationError):
			recreate_workspace(self.ws_map.name)

	def test_recreate_channel_rejects_an_unknown_mapping(self):
		from raven_integration.api import recreate_channel

		with self.assertRaises(frappe.DoesNotExistError):
			recreate_channel("RCM-does-not-exist")

	def test_recreate_channel_rejects_a_non_string_name(self):
		from raven_integration.api import recreate_channel

		with self.assertRaises(frappe.ValidationError):
			recreate_channel({"name": self.ch_map.name})


class TestRecreatePermissions(_StaleFixtureMixin, FrappeTestCase):
	def setUp(self):
		self._setUp_mappings()
		self.non_admin = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"rv-stale-{self.suffix}@example.com",
				"first_name": "StalePlain",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(lambda: frappe.db.delete("User", {"name": self.non_admin.name}))

	def test_recreate_endpoints_require_system_manager(self):
		from raven_integration.api import recreate_channel, recreate_workspace

		self._delete_raven_channel()
		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				recreate_channel(self.ch_map.name)
			with self.assertRaises(frappe.PermissionError):
				recreate_workspace(self.ws_map.name)
		finally:
			frappe.set_user("Administrator")


class TestDeleteNeverTouchesRaven(_StaleFixtureMixin, FrappeTestCase):
	"""This app deletes its own mappings and rules and nothing else."""

	def setUp(self):
		self._setUp_mappings()

	def test_delete_workspace_keeps_the_raven_workspace(self):
		from raven_integration.api import delete_workspace

		raven_workspace = self.ws_map.raven_workspace
		delete_workspace(name=self.ws_map.name)

		self.assertFalse(frappe.db.exists("Raven Workspace Mapping", self.ws_map.name))
		self.assertTrue(frappe.db.exists("Raven Workspace", raven_workspace))

	def test_delete_workspace_keeps_the_raven_channels_under_it(self):
		from raven_integration.api import delete_workspace

		raven_channel = self.ch_map.raven_channel
		delete_workspace(name=self.ws_map.name)

		self.assertFalse(frappe.db.exists("Raven Channel Mapping", self.ch_map.name))
		self.assertTrue(frappe.db.exists("Raven Channel", raven_channel))

	def test_delete_channel_keeps_the_raven_channel(self):
		from raven_integration.api import delete_channel

		raven_channel = self.ch_map.raven_channel
		delete_channel(name=self.ch_map.name)

		self.assertFalse(frappe.db.exists("Raven Channel Mapping", self.ch_map.name))
		self.assertTrue(frappe.db.exists("Raven Channel", raven_channel))

	def test_delete_endpoints_expose_no_raven_delete_switch(self):
		"""Regression: the removed delete_raven parameter must not come back."""
		from raven_integration.api import delete_channel, delete_workspace

		for fn in (delete_workspace, delete_channel):
			self.assertEqual(list(inspect.signature(fn).parameters), ["name"])


class TestNightlySweepDoesNotBlockRecreate(_StaleFixtureMixin, FrappeTestCase):
	"""The nightly backstop must mark mappings stale, never clear `enabled`.

	detect_dangling_links used to clear `enabled`, and recreate_* only clears
	`stale` — so within 24h of any Raven-side delete, recreating the record left
	the mapping disabled and it silently never resumed syncing.
	"""

	def setUp(self):
		self._setUp_mappings(with_rule=True)

	def tearDown(self):
		self._tearDown_mappings()

	def test_sweep_marks_stale_and_leaves_enabled_alone(self):
		from raven_integration.scheduler import detect_dangling_links

		self._delete_raven_workspace()
		frappe.db.set_value("Raven Workspace Mapping", self.ws_map.name, "stale", 0)
		result = detect_dangling_links()
		self.assertGreaterEqual(result.get("flagged_stale", 0), 1)
		stale, enabled = frappe.db.get_value(
			"Raven Workspace Mapping", self.ws_map.name, ["stale", "enabled"]
		)
		self.assertEqual(stale, 1)
		self.assertEqual(enabled, 1, "the sweep must not disable — recreate only clears stale")

	def test_recreate_after_the_sweep_resumes_syncing(self):
		from raven_integration.api import recreate_workspace
		from raven_integration.scheduler import detect_dangling_links
		from raven_integration.sync_service import sync_workspace_members

		self._delete_raven_workspace()
		frappe.db.set_value("Raven Workspace Mapping", self.ws_map.name, "stale", 0)
		detect_dangling_links()
		recreate_workspace(name=self.ws_map.name)
		result = sync_workspace_members(self.ws_map.name)
		self.assertNotIn(
			result.get("reason"),
			("disabled", "raven_record_deleted"),
			f"syncing must resume after recreate, got {result}",
		)
