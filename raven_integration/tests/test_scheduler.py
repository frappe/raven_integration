from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration.scheduler import detect_dangling_links, reconcile_all


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

	It must set `stale`, never clear `enabled` — `enabled` is the user's own choice
	to keep syncing, and recreate only clears `stale`, so disabling here would
	leave a recreated mapping silently unsynced."""

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
			stale, enabled = frappe.db.get_value("Raven Workspace Mapping", ws_map.name, ["stale", "enabled"])
			self.assertEqual(stale, 1, "mapping with a missing Raven Workspace must be marked stale=1")
			self.assertEqual(
				enabled, 1, "the sweep must not disable the mapping — recreate only clears stale"
			)
		finally:
			frappe.db.delete("Raven Workspace Mapping", {"name": ws_map.name})

	def test_active_workspace_with_valid_link_not_disabled(self):
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
			enabled = frappe.db.get_value("Raven Workspace Mapping", ws_map.name, "enabled")
			self.assertNotEqual(
				enabled,
				0,
				"Raven Workspace Mapping with a valid Raven Workspace link must NOT be disabled",
			)
		finally:
			frappe.db.delete("Raven Workspace Mapping", {"name": ws_map.name})
			frappe.db.delete("Raven Workspace", {"name": raven_ws.name})
