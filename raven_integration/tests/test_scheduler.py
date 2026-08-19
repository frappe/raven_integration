from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import hooks
from raven_integration.scheduler import detect_dangling_links, reconcile_all


class TestReconcileSchedule(FrappeTestCase):
	"""The nightly reconcile has to be queued somewhere it can finish.

	A timeout rolls the job back, throwing away every add and remove of the sweep AND
	the stale flags detect_dangling_links set, with no retry until the next night. The
	scheduler picks the queue from the frequency alone, and the frequency comes from
	the scheduler_events key.
	"""

	METHOD = "raven_integration.scheduler.reconcile_all"

	def frequency(self) -> str:
		keys = [
			key
			for key, methods in hooks.scheduler_events.items()
			if not isinstance(methods, dict) and self.METHOD in methods
		]
		self.assertEqual(len(keys), 1, f"{self.METHOD} must be scheduled exactly once")
		# What insert_event_jobs() makes of the hook key.
		return keys[0].replace("_", " ").title()

	def test_the_hook_key_names_a_frequency_this_frappe_version_has(self):
		options = frappe.get_meta("Scheduled Job Type").get_field("frequency").options.split("\n")
		self.assertIn(self.frequency(), options)

	def test_reconcile_is_queued_where_it_has_time_to_finish(self):
		from frappe.utils.background_jobs import get_queues_timeout

		job = frappe.new_doc("Scheduled Job Type", method=self.METHOD, frequency=self.frequency())
		queue = job.get_queue_name()
		self.assertEqual(queue, "long", "a whole-site sweep does not fit the default queue")
		timeouts = get_queues_timeout()
		self.assertGreater(timeouts[queue], timeouts["default"])


class TestReconcile(FrappeTestCase):
	"""Tests for reconcile_all(): status tracking and skip behaviour."""

	def setUp(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		# reconcile_all() now requires the integration to be enabled (is_active).
		# Enable it for the sweep, snapshotting the shared single to restore after.
		self._prev_enabled = frappe.db.get_single_value("Raven Membership Settings", "enabled")
		frappe.db.set_single_value("Raven Membership Settings", "enabled", 1)

	def tearDown(self):
		# Restore the enabled flag so other tests see defaults.
		frappe.db.set_single_value("Raven Membership Settings", "enabled", self._prev_enabled)

	def test_reconcile_all_returns_summary(self):
		"""reconcile_all() returns a summary dict with the expected counters."""
		result = reconcile_all()

		for key in ("channels_processed", "workspaces_processed", "added", "removed", "errors"):
			self.assertIn(key, result, f"result must have {key} key")

	def test_disabled_records_skipped(self):
		"""reconcile_all() does not call sync_channel_members for disabled Raven Channel Mappings."""
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")

		suffix = frappe.generate_hash(length=6)

		# Parent workspace mapping first (skip_raven_create to avoid real Raven calls).
		ws_map = frappe.new_doc(
			"Raven Workspace Mapping",
			workspace_label=f"Sched Test WS {suffix}",
			workspace_type="Private",
		)
		ws_map.flags.skip_raven_create = True
		ws_map.insert()

		ch_map = frappe.new_doc(
			"Raven Channel Mapping",
			channel_label=f"Sched Test Ch {suffix}",
			workspace=ws_map.name,
			channel_type="Private",
		)
		ch_map.flags.skip_raven_create = True
		ch_map.insert()
		# Clear `enabled` so reconcile_all should skip it.
		frappe.db.set_value("Raven Channel Mapping", ch_map.name, "enabled", 0)

		try:
			with patch(
				"raven_integration.events.sync_channel_members",
				wraps=MagicMock(return_value={"added": 0, "removed": 0}),
			) as mock_sync:
				reconcile_all()
				called_with = [call.args[0] for call in mock_sync.call_args_list]
				self.assertNotIn(
					ch_map.name,
					called_with,
					"sync_channel_members must NOT be called for a disabled channel",
				)
		finally:
			frappe.db.delete("Raven Channel Mapping", {"name": ch_map.name})
			frappe.db.delete("Raven Workspace Mapping", {"name": ws_map.name})


class TestDanglingLinks(FrappeTestCase):
	"""Tests for detect_dangling_links(): stale-on-missing-Raven-doc behaviour.

	It must set `stale` and nothing else. A workspace mapping has no `enabled` to
	clear; a channel's is the user's own choice to keep syncing, and recreate only
	clears `stale`, so disabling here would leave a recreated mapping silently
	unsynced."""

	def setUp(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")

	def test_dangling_raven_workspace_link_marks_stale(self):
		"""A Raven Workspace Mapping whose raven_workspace no longer exists is marked stale=1."""
		suffix = frappe.generate_hash(length=6)

		ws_map = frappe.new_doc(
			"Raven Workspace Mapping",
			workspace_label=f"Ghost WS Test {suffix}",
			workspace_type="Private",
		)
		ws_map.flags.skip_raven_create = True
		ws_map.insert()
		# Bypass field validation and point at a non-existent Raven Workspace.
		frappe.db.set_value("Raven Workspace Mapping", ws_map.name, "raven_workspace", "ghost-ws")

		try:
			result = detect_dangling_links()
			self.assertGreaterEqual(
				result.get("flagged_stale", 0),
				1,
				"detect_dangling_links must flag at least one record",
			)
			stale = frappe.db.get_value("Raven Workspace Mapping", ws_map.name, "stale")
			self.assertEqual(stale, 1, "mapping with a missing Raven Workspace must be marked stale=1")
		finally:
			frappe.db.delete("Raven Workspace Mapping", {"name": ws_map.name})

	def test_workspace_with_valid_link_is_not_marked_stale(self):
		"""A Raven Workspace Mapping whose raven_workspace exists is left untouched."""
		suffix = frappe.generate_hash(length=6)

		raven_ws = frappe.get_doc(
			{
				"doctype": "Raven Workspace",
				"workspace_name": f"Valid WS {suffix}",
				"type": "Private",
			}
		).insert(ignore_permissions=True)

		ws_map = frappe.new_doc(
			"Raven Workspace Mapping",
			workspace_label=f"Valid WS Map {suffix}",
			workspace_type="Private",
		)
		ws_map.flags.skip_raven_create = True
		ws_map.insert()
		frappe.db.set_value("Raven Workspace Mapping", ws_map.name, "raven_workspace", raven_ws.name)

		try:
			detect_dangling_links()
			stale = frappe.db.get_value("Raven Workspace Mapping", ws_map.name, "stale")
			self.assertNotEqual(
				stale,
				1,
				"Raven Workspace Mapping with a valid Raven Workspace link must NOT be marked stale",
			)
		finally:
			frappe.db.delete("Raven Workspace Mapping", {"name": ws_map.name})
			frappe.db.delete("Raven Workspace", {"name": raven_ws.name})
