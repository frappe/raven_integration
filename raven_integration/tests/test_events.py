from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import registry
from raven_integration.events import (
	_DEBOUNCE_KEY,
	_TRIGGER_DOCTYPES_KEY,
	_trigger_doctypes,
	clear_trigger_doctypes_cache,
	is_active,
	notify_change,
	on_provider_doc_change,
	resync_all,
	run_resync,
)
from raven_integration.exceptions import RavenAPIError

_FAKE_PROVIDER_PATH = "raven_integration.tests.fake_provider.get_provider"


class TestIsActive(FrappeTestCase):
	def test_active_only_when_enabled_and_raven_installed(self):
		# is_active() is true ONLY when the integration is enabled AND "raven"
		# is installed; false on either condition failing.
		cases = [
			(True, ["raven"], True),
			(True, ["raven", "lms"], True),
			(True, ["lms"], False),
			(True, [], False),
			(False, ["raven"], False),
			(False, ["lms"], False),
			(0, ["raven"], False),
			(1, ["raven", "lms"], True),
		]
		for enabled, apps, expected in cases:
			with (
				patch(
					"raven_integration.events.frappe.db.get_single_value",
					return_value=enabled,
				),
				patch("raven_integration.utils.frappe.get_installed_apps", return_value=apps),
			):
				self.assertEqual(is_active(), expected, f"enabled={enabled} apps={apps}")


class TestResyncAll(FrappeTestCase):
	def _get_all(self, channels, workspaces):
		"""Fake frappe.get_all. ``channels`` is a list of (name, parent workspace) pairs."""

		def fake(doctype, **kw):
			# Contract: the sweep only ever pulls active (enabled=1) mappings.
			self.assertEqual(kw.get("filters"), {"enabled": 1})
			if "Channel" in doctype:
				# The parent mapping is needed to skip orphaned channels.
				self.assertEqual(kw.get("fields"), ["name", "workspace"])
				return [frappe._dict(name=n, workspace=w) for n, w in channels]
			return list(workspaces)

		return fake

	def test_aggregates_counts_over_active_mappings(self):
		with (
			patch(
				"raven_integration.events.frappe.get_all",
				side_effect=self._get_all([("c1", "w1"), ("c2", "w1")], ["w1"]),
			),
			patch(
				"raven_integration.events.sync_channel_members", return_value={"added": 2, "removed": 1}
			) as mc,
			patch(
				"raven_integration.events.sync_workspace_members", return_value={"added": 3, "removed": 0}
			) as mw,
		):
			summary = resync_all()

		self.assertEqual(
			summary,
			{
				"channels_processed": 2,
				"channels_skipped_disabled_workspace": 0,
				"workspaces_processed": 1,
				"added": 2 + 2 + 3,
				"removed": 1 + 1 + 0,
				"errors": 0,
			},
		)
		self.assertEqual(mc.call_count, 2)
		self.assertEqual(mw.call_count, 1)

	def test_per_record_error_is_counted_and_does_not_abort(self):
		with (
			patch(
				"raven_integration.events.frappe.get_all", side_effect=self._get_all([("c1", "w1")], ["w1"])
			),
			patch("raven_integration.events.sync_channel_members", side_effect=RavenAPIError("boom")),
			patch("raven_integration.events.sync_workspace_members", return_value={}),
			patch("raven_integration.events.frappe.log_error") as log,
		):
			summary = resync_all()

		self.assertEqual(summary["errors"], 1)
		self.assertEqual(summary["channels_processed"], 1)
		log.assert_called()

	def test_channel_under_a_disabled_workspace_mapping_is_not_swept(self):
		# Syncing a channel also joins its members to the parent Raven workspace,
		# but a disabled workspace mapping is excluded from the sweep — so those
		# workspace rows would accumulate with nothing left to reconcile them.
		# "w2" is absent from the active-workspace list, i.e. disabled.
		with (
			patch(
				"raven_integration.events.frappe.get_all",
				side_effect=self._get_all([("c1", "w1"), ("orphan", "w2")], ["w1"]),
			),
			patch("raven_integration.events.sync_channel_members", return_value={}) as mc,
			patch("raven_integration.events.sync_workspace_members", return_value={}),
		):
			summary = resync_all()

		self.assertEqual([c.args[0] for c in mc.call_args_list], ["c1"])
		self.assertEqual(summary["channels_processed"], 1)
		self.assertEqual(summary["channels_skipped_disabled_workspace"], 1)

	def test_channels_are_swept_before_workspaces(self):
		# add_channel_member joins the parent workspace before the channel, so the
		# teardown must mirror it: drop the channel row first. Sweeping workspaces
		# first would evict a member from the workspace while their channel row is
		# still there, and resync_all swallows per-record errors and carries on, so
		# that half-torn-down state can outlive the sweep.
		order = []

		def record(kind):
			def sync(name, **kw):
				order.append(kind)
				return {}

			return sync

		with (
			patch(
				"raven_integration.events.frappe.get_all",
				side_effect=self._get_all([("c1", "w1")], ["w1"]),
			),
			patch("raven_integration.events.sync_channel_members", side_effect=record("channel")),
			patch("raven_integration.events.sync_workspace_members", side_effect=record("workspace")),
		):
			resync_all()

		self.assertEqual(order, ["channel", "workspace"])


class TestNotifyChange(FrappeTestCase):
	def setUp(self):
		frappe.cache().delete_value(_DEBOUNCE_KEY)
		self.addCleanup(lambda: frappe.cache().delete_value(_DEBOUNCE_KEY))

	def test_noop_when_inactive(self):
		with (
			patch("raven_integration.events.is_active", return_value=False),
			patch("raven_integration.events.frappe.enqueue") as enq,
		):
			notify_change()
		enq.assert_not_called()

	def test_enqueues_once_then_debounces(self):
		with (
			patch("raven_integration.events.is_active", return_value=True),
			patch("raven_integration.events.frappe.enqueue") as enq,
		):
			notify_change()
			notify_change()
		self.assertEqual(enq.call_count, 1, "second call within the window must be debounced")

	def test_rolled_back_transaction_does_not_suppress_the_next_sync(self):
		# The debounce key is written straight to redis, but the sweep it guards is
		# only queued on commit. If a rollback left the key behind, every change for
		# the rest of the 30s window would be dropped with no sweep ever scheduled.
		with (
			patch("raven_integration.events.is_active", return_value=True),
			patch("raven_integration.events.frappe.enqueue"),
		):
			notify_change()
		self.assertTrue(frappe.cache().get_value(_DEBOUNCE_KEY))

		frappe.db.after_rollback.run()  # what the framework does on rollback
		self.assertIsNone(frappe.cache().get_value(_DEBOUNCE_KEY))

		with (
			patch("raven_integration.events.is_active", return_value=True),
			patch("raven_integration.events.frappe.enqueue") as enq,
		):
			notify_change()
		enq.assert_called_once()


class TestRunResync(FrappeTestCase):
	def test_clears_debounce_key_and_sweeps(self):
		frappe.cache().set_value(_DEBOUNCE_KEY, "1")
		with patch("raven_integration.events.resync_all") as rs:
			run_resync()
		rs.assert_called_once()
		self.assertIsNone(frappe.cache().get_value(_DEBOUNCE_KEY))


class TestTriggerDoctypes(FrappeTestCase):
	def test_collects_triggers_from_registered_providers(self):
		with patch.object(registry, "_provider_paths", return_value=[_FAKE_PROVIDER_PATH]):
			doctypes = registry.trigger_doctypes()
		self.assertIn("Some Doctype", doctypes)
		self.assertIn("Another Doctype", doctypes)


class TestTriggerDoctypeCache(FrappeTestCase):
	def setUp(self):
		clear_trigger_doctypes_cache()
		self.addCleanup(clear_trigger_doctypes_cache)

	def test_registry_is_consulted_once_per_cache_fill(self):
		with patch.object(registry, "trigger_doctypes", return_value={"Some Doctype"}) as tr:
			_trigger_doctypes()
			_trigger_doctypes()
		self.assertEqual(tr.call_count, 1)

	def test_a_newly_installed_providers_triggers_are_picked_up(self):
		# Without invalidation the memo outlives the install and the new provider's
		# doctypes never reach on_provider_doc_change.
		with patch.object(registry, "trigger_doctypes", return_value={"Old Doctype"}):
			self.assertEqual(_trigger_doctypes(), {"Old Doctype"})

		clear_trigger_doctypes_cache()

		with patch.object(registry, "trigger_doctypes", return_value={"Old Doctype", "New Doctype"}):
			self.assertEqual(_trigger_doctypes(), {"Old Doctype", "New Doctype"})

	def test_cached_entry_expires_on_its_own(self):
		# The TTL is the backstop for anything that bypasses the invalidation hooks.
		with patch.object(registry, "trigger_doctypes", return_value={"Some Doctype"}):
			_trigger_doctypes()
		cache = frappe.cache()
		self.assertGreater(cache.ttl(cache.make_key(_TRIGGER_DOCTYPES_KEY)), 0)

	def test_invalidation_is_wired_to_the_install_and_app_hooks(self):
		if "raven_integration" not in frappe.get_installed_apps():
			self.skipTest("raven_integration not installed on this site")
		path = "raven_integration.events.clear_trigger_doctypes_cache"
		for hook in ("after_install", "after_migrate", "after_app_install", "after_app_uninstall"):
			self.assertIn(path, frappe.get_hooks(hook), f"{hook} must invalidate the trigger cache")

	def test_after_raven_member_synced_is_declared(self):
		# sync_service fires this hook; an undeclared hook is an undocumented one.
		from raven_integration import hooks

		self.assertEqual(hooks.after_raven_member_synced, [])


class TestOnProviderDocChange(FrappeTestCase):
	def test_notifies_when_doctype_is_a_trigger(self):
		doc = frappe._dict(doctype="LMS Enrollment")
		with (
			patch("raven_integration.events._trigger_doctypes", return_value={"LMS Enrollment"}),
			patch("raven_integration.events.notify_change") as notify,
		):
			on_provider_doc_change(doc)
		notify.assert_called_once()

	def test_ignores_doctype_that_is_not_a_trigger(self):
		doc = frappe._dict(doctype="ToDo")
		with (
			patch("raven_integration.events._trigger_doctypes", return_value={"LMS Enrollment"}),
			patch("raven_integration.events.notify_change") as notify,
		):
			on_provider_doc_change(doc)
		notify.assert_not_called()

	def test_never_raises_when_trigger_lookup_throws(self):
		doc = frappe._dict(doctype="LMS Enrollment")
		with (
			patch("raven_integration.events._trigger_doctypes", side_effect=RuntimeError("boom")),
			patch("raven_integration.events.notify_change") as notify,
			patch("raven_integration.events.frappe.log_error") as log,
		):
			on_provider_doc_change(doc)  # must not raise
		notify.assert_not_called()
		log.assert_called_once()
