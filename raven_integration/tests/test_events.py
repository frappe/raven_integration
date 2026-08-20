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
from raven_integration.exceptions import ProviderDataError, RavenAPIError
from raven_integration.tests import hold_one_transaction
from raven_integration.utils import raven_installed

_FAKE_PROVIDER_PATH = "raven_integration.tests.fake_provider.get_provider"


class TestRavenInstalled(FrappeTestCase):
	def test_a_disabled_raven_does_not_count(self):
		# `bench disable-app raven` leaves raven in get_installed_apps() with its tables
		# intact, and takes it out of get_active_apps() — the list frappe itself uses to
		# resolve hooks and to skip a disabled app's scheduled jobs. Reading the wrong
		# one has the nightly sweep inserting and deleting member rows in an app the
		# site has switched off.
		with (
			patch("raven_integration.utils.frappe.get_installed_apps", return_value=["raven"]),
			patch("raven_integration.utils.frappe.get_active_apps", return_value=[]),
		):
			self.assertFalse(raven_installed())

	def test_an_active_raven_counts(self):
		with patch("raven_integration.utils.frappe.get_active_apps", return_value=["raven", "lms"]):
			self.assertTrue(raven_installed())


class TestIsActive(FrappeTestCase):
	def test_active_only_when_enabled_and_raven_installed(self):
		# is_active() is true ONLY when the integration is enabled AND "raven"
		# is active; false on either condition failing.
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
				patch("raven_integration.utils.frappe.get_active_apps", return_value=apps),
			):
				self.assertEqual(is_active(), expected, f"enabled={enabled} apps={apps}")


class TestResyncAll(FrappeTestCase):
	def setUp(self):
		hold_one_transaction(self)

	def _get_all(self, channels, workspaces):
		"""Fake frappe.get_all. ``channels`` is a list of (name, parent workspace) pairs."""

		def fake(doctype, **kw):
			if "Channel" in doctype:
				# Contract: only enabled channels are swept, and the parent mapping is
				# pulled with them so an orphaned channel can be skipped.
				self.assertEqual(kw.get("filters"), {"enabled": 1})
				self.assertEqual(kw.get("fields"), ["name", "workspace"])
				return [frappe._dict(name=n, workspace=w) for n, w in channels]
			# Workspaces are pulled unfiltered: a workspace mapping has no `enabled`.
			self.assertIsNone(kw.get("filters"))
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
				"channels_skipped_orphaned": 0,
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

	def test_channel_whose_workspace_mapping_is_gone_is_not_swept(self):
		# Syncing a channel also joins its members to the parent Raven workspace, so
		# a channel whose workspace mapping no longer exists would strand workspace
		# rows with nothing left to reconcile them. Deleting a workspace cascades
		# its channels, so this is the guard for a cascade that did not finish.
		# "w2" is absent from the workspace list, i.e. its mapping is gone.
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
		self.assertEqual(summary["channels_skipped_orphaned"], 1)

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


class TestChangeDebouncedAgainstAnInFlightSweep(FrappeTestCase):
	"""A change told "a sweep is already coming" must be covered by a sweep.

	notify_change() returns without queuing anything while the debounce key is set, but
	the sweep that key stands for is one transaction under REPEATABLE READ: rows
	committed after it took its snapshot are invisible to it. So the very sweep the
	change was debounced against can be the one that misses it, and nothing else runs
	until an unrelated change or the nightly reconcile.
	"""

	def setUp(self):
		frappe.cache().delete_value(_DEBOUNCE_KEY)
		self.addCleanup(lambda: frappe.cache().delete_value(_DEBOUNCE_KEY))

	def test_a_change_committing_mid_sweep_gets_a_sweep_of_its_own(self):
		with (
			patch("raven_integration.events.is_active", return_value=True),
			patch("raven_integration.events.frappe.enqueue") as enq,
		):
			notify_change()  # first request: sets the key, queues the sweep
			frappe.db.after_commit.run()  # ...and commits, so the sweep will see its rows
			notify_change()  # second request: debounced, queues nothing of its own
			enq.reset_mock()

			def sweep(*args, **kwargs):
				# The second request commits while the sweep is in flight, so its rows
				# are not in the snapshot this sweep is reading.
				frappe.db.after_commit.run()

			with patch("raven_integration.events.resync_all", side_effect=sweep):
				run_resync()

			enq.assert_called_once()

	def test_a_quiet_sweep_does_not_reschedule_itself(self):
		# The re-check must not turn every sweep into an endless chain of sweeps.
		with (
			patch("raven_integration.events.is_active", return_value=True),
			patch("raven_integration.events.frappe.enqueue") as enq,
		):
			notify_change()
			frappe.db.after_commit.run()
			enq.reset_mock()
			with patch("raven_integration.events.resync_all"):
				run_resync()
			enq.assert_not_called()


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

	def test_a_registry_that_blows_up_is_logged(self):
		# Failing closed is right — but silently, this is a site-wide no-op: every save
		# stops reaching notify_change() and nothing says why until the nightly sweep.
		with (
			patch.object(registry, "trigger_doctypes", side_effect=RuntimeError("boom")),
			patch("raven_integration.events.frappe.log_error") as log,
		):
			self.assertEqual(_trigger_doctypes(), set())
		log.assert_called_once()

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


class TestSweepTransactionBoundaries(FrappeTestCase):
	"""Where resync_all ends one mapping's transaction and starts the next.

	The sweep used to be one transaction for the whole site. Every lock it took was
	held to the end of the night — including the gap locks remove_workspace_member's
	locking read takes on Raven Channel Member, which match nothing by construction —
	and a queue timeout rolled back the entire run, the stale flags scheduler.
	reconcile_all had just set along with it.
	"""

	def _get_all(self, channels, workspaces):
		def fake(doctype, **kw):
			if doctype == "Raven Channel Mapping":
				return [frappe._dict(name=c, workspace=w) for c, w in channels]
			return list(workspaces)

		return fake

	def _sweep(self, *, channel_sync):
		with (
			patch(
				"raven_integration.events.frappe.get_all",
				side_effect=self._get_all([("c1", "w1"), ("c2", "w1")], ["w1"]),
			),
			patch("raven_integration.events.sync_channel_members", side_effect=channel_sync),
			patch("raven_integration.events.sync_workspace_members", return_value={}),
			patch("raven_integration.events.frappe.log_error"),
			patch("raven_integration.events._commit_step") as commit,
			patch("raven_integration.events._rollback_step") as rollback,
		):
			resync_all()
		return commit, rollback

	def test_every_mapping_gets_its_own_commit(self):
		# Two channels and one workspace, plus the commit before the loop that makes
		# the caller's own writes durable rather than rollback fodder.
		commit, rollback = self._sweep(channel_sync=lambda name, **kw: {})
		self.assertEqual(commit.call_count, 4)
		rollback.assert_not_called()

	def test_the_sweep_commits_before_it_takes_its_first_lock(self):
		# scheduler.reconcile_all marks dangling links stale immediately before
		# calling this, and a mapping that fails first would otherwise roll those
		# flags back — the one output of the run that stops a mapping syncing
		# against a Raven record that is gone.
		calls = []
		with (
			patch(
				"raven_integration.events.frappe.get_all",
				side_effect=self._get_all([("c1", "w1")], ["w1"]),
			),
			patch(
				"raven_integration.events.sync_channel_members",
				side_effect=lambda name, **kw: (calls.append("sync"), {})[1],
			),
			patch("raven_integration.events.sync_workspace_members", return_value={}),
			patch("raven_integration.events._commit_step", side_effect=lambda: calls.append("commit")),
			patch("raven_integration.events._rollback_step"),
		):
			resync_all()
		self.assertEqual(calls[0], "commit")

	def test_a_failed_mapping_is_rolled_back_before_the_next_one_commits(self):
		# Without the rollback the next mapping's commit adopts this one's
		# half-applied diff, which is the state the per-member savepoints exist to
		# keep out of the database.
		order = []

		def sync(name, **kw):
			order.append(f"sync:{name}")
			if name == "c1":
				raise RavenAPIError("boom")
			return {}

		with (
			patch(
				"raven_integration.events.frappe.get_all",
				side_effect=self._get_all([("c1", "w1"), ("c2", "w1")], ["w1"]),
			),
			patch("raven_integration.events.sync_channel_members", side_effect=sync),
			patch("raven_integration.events.sync_workspace_members", return_value={}),
			patch("raven_integration.events.frappe.log_error"),
			patch("raven_integration.events._commit_step", side_effect=lambda: order.append("commit")),
			patch("raven_integration.events._rollback_step", side_effect=lambda: order.append("rollback")),
		):
			resync_all()

		self.assertEqual(order[:4], ["commit", "sync:c1", "rollback", "commit"])

	def test_the_error_log_survives_the_rollback_that_precedes_it(self):
		# log_error writes a document. Rolling back after it would discard the only
		# record that the mapping failed at all.
		order = []

		with (
			patch(
				"raven_integration.events.frappe.get_all",
				side_effect=self._get_all([("c1", "w1")], ["w1"]),
			),
			patch(
				"raven_integration.events.sync_channel_members",
				side_effect=ProviderDataError("no provider"),
			),
			patch("raven_integration.events.sync_workspace_members", return_value={}),
			patch("raven_integration.events.frappe.log_error", side_effect=lambda **kw: order.append("log")),
			patch("raven_integration.events._commit_step", side_effect=lambda: order.append("commit")),
			patch("raven_integration.events._rollback_step", side_effect=lambda: order.append("rollback")),
		):
			resync_all()

		self.assertEqual(order[1:4], ["rollback", "log", "commit"])
