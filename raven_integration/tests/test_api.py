from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration import registry


class TestSetupGate(FrappeTestCase):
	def setUp(self):
		self.non_admin = frappe.get_doc(
			{
				"doctype": "User",
				"email": "rv-setup-non-admin@example.com",
				"first_name": "SetupPlain",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(self.non_admin.delete)

	def test_is_setup_returns_dict_with_bool_keys(self):
		from raven_integration.api import is_setup

		result = is_setup()
		self.assertIsInstance(result, dict)
		self.assertIn("raven", result)
		self.assertIn("raven_integration", result)
		self.assertIsInstance(result["raven"], bool)
		self.assertIsInstance(result["raven_integration"], bool)
		# Both apps are installed on this bench
		self.assertTrue(result["raven"])
		self.assertTrue(result["raven_integration"])
		# is_setup also reports whether the integration has been enabled.
		self.assertIn("enabled", result)
		self.assertIsInstance(result["enabled"], bool)

	def test_is_setup_requires_system_manager(self):
		"""is_setup enumerates installed apps, so it must not answer plain users."""
		from raven_integration.api import is_setup

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				is_setup()
		finally:
			frappe.set_user("Administrator")

	def test_list_providers_returns_list(self):
		from raven_integration.api import list_providers

		result = list_providers()
		self.assertIsInstance(result, list)


class TestEnableIntegration(FrappeTestCase):
	"""enable_integration — one-way enable (no disable endpoint exists)."""

	def setUp(self):
		# Snapshot the shared single's enabled flag so we can restore it; mutating
		# it here must not leak into other tests that read is_active().
		self._prev_enabled = frappe.db.get_single_value("Raven Membership Settings", "enabled")
		self.non_admin = frappe.get_doc(
			{
				"doctype": "User",
				"email": "rv-enable-non-admin@example.com",
				"first_name": "EnablePlain",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(self.non_admin.delete)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Raven Membership Settings", "enabled", self._prev_enabled)

	def test_enable_integration_sets_flag_and_returns_enabled(self):
		from raven_integration.api import enable_integration, is_setup

		frappe.db.set_single_value("Raven Membership Settings", "enabled", 0)
		# Runs as Administrator (a System Manager). Patch enqueue so no job is queued.
		with patch("raven_integration.api.frappe.enqueue") as enq:
			result = enable_integration()
		self.assertEqual(result, {"enabled": True})
		self.assertEqual(frappe.db.get_single_value("Raven Membership Settings", "enabled"), 1)
		enq.assert_called_once()
		# is_setup now reflects the enabled state.
		self.assertTrue(is_setup()["enabled"])

	def test_enable_integration_requires_system_manager(self):
		from raven_integration.api import enable_integration

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				enable_integration()
		finally:
			frappe.set_user("Administrator")


class TestWorkspaceAPI(FrappeTestCase):
	def setUp(self):
		self.non_admin = frappe.get_doc(
			{
				"doctype": "User",
				"email": "rv-non-admin@example.com",
				"first_name": "Plain",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(self.non_admin.delete)

	def test_list_workspaces_requires_system_manager(self):
		from raven_integration.api import list_workspaces

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				list_workspaces()
		finally:
			frappe.set_user("Administrator")

	def test_create_workspace_rejects_non_string_label(self):
		from raven_integration.api import create_workspace

		with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
			create_workspace(label=42, type="Private")

	def test_create_workspace_rejects_invalid_type(self):
		from raven_integration.api import create_workspace

		with self.assertRaises(frappe.ValidationError):
			create_workspace(label="Test", type="weird")

	def test_create_workspace_persists_and_returns_name(self):
		from raven_integration.api import create_workspace

		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		with patch.object(registry, "_provider_paths", return_value=[]):
			name = create_workspace(label="API Test WS", type="Private")
		self.assertTrue(name.startswith("RWM-"))
		self.addCleanup(lambda: frappe.delete_doc("Raven Workspace Mapping", name, force=True))

	def test_create_workspace_auto_names_when_label_omitted(self):
		from raven_integration.api import create_workspace

		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		with patch.object(registry, "_provider_paths", return_value=[]):
			a = create_workspace()
			b = create_workspace()
		for n in (a, b):
			self.addCleanup(
				lambda n=n: frappe.delete_doc("Raven Workspace Mapping", n, force=True, ignore_missing=True)
			)
		label_a = frappe.db.get_value("Raven Workspace Mapping", a, "workspace_label")
		label_b = frappe.db.get_value("Raven Workspace Mapping", b, "workspace_label")
		self.assertRegex(label_a, r"^Workspace \d+$")
		self.assertRegex(label_b, r"^Workspace \d+$")
		self.assertNotEqual(label_a, label_b, "consecutive auto-creates must differ")

	def test_create_workspace_takes_no_rules_or_combinator(self):
		# Rules live on channels; a workspace endpoint that still accepted them would
		# store something nothing evaluates.
		from raven_integration.api import create_workspace

		with self.assertRaises(TypeError):
			create_workspace(label="Bad", type="Private", rules=None)
		with self.assertRaises(TypeError):
			create_workspace(label="Bad", type="Private", combinator="All (AND)")

	def test_get_workspace_serves_no_rules(self):
		from raven_integration.api import create_workspace, get_workspace

		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		with patch.object(registry, "_provider_paths", return_value=[]):
			name = create_workspace(label="No Rules WS", type="Private")
		self.addCleanup(lambda: frappe.delete_doc("Raven Workspace Mapping", name, force=True))
		detail = get_workspace(name)
		self.assertNotIn("member_rules", detail)
		self.assertNotIn("member_rules_json", detail)
		self.assertNotIn("rules", detail)
		self.assertNotIn("rule_combinator", detail)

	def test_update_workspace_rejects_unknown_name(self):
		from raven_integration.api import update_workspace

		with self.assertRaises(frappe.DoesNotExistError):
			update_workspace(
				name="RWM-does-not-exist",
				label="New Label",
				type="Private",
			)

	def test_delete_workspace_rejects_unknown_name(self):
		from raven_integration.api import delete_workspace

		with self.assertRaises(frappe.DoesNotExistError):
			delete_workspace(name="RWM-does-not-exist")

	def test_delete_workspace_with_channel_cascades(self):
		"""delete_workspace on a workspace that has a child channel succeeds
		(no LinkExistsError) and removes the channel too."""
		from raven_integration.api import delete_workspace

		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = "API Cascade Delete WS"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", ws.name, force=True, ignore_missing=True)
		)

		ch = frappe.new_doc("Raven Channel Mapping")
		ch.channel_label = "API Cascade Delete Channel"
		ch.workspace = ws.name
		ch.channel_type = "Private"
		ch.flags.skip_raven_create = True
		ch.insert()
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", ch.name, force=True, ignore_missing=True)
		)

		# Runs as Administrator (a System Manager), satisfying _require_manager.
		delete_workspace(name=ws.name)

		self.assertFalse(frappe.db.exists("Raven Workspace Mapping", ws.name))
		self.assertFalse(frappe.db.exists("Raven Channel Mapping", ch.name))

	def test_delete_workspace_keeps_raven_object_by_default(self):
		"""Default delete removes only the mapping; the backing Raven Workspace stays."""
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		from raven_integration.api import delete_workspace

		suffix = frappe.generate_hash(length=6)
		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = f"API Del Keep WS {suffix}"
		ws.workspace_type = "Private"
		ws.insert()  # creates a real backing Raven Workspace
		rw = ws.raven_workspace
		self.assertTrue(frappe.db.exists("Raven Workspace", rw))
		self.addCleanup(
			lambda: frappe.delete_doc(
				"Raven Workspace", rw, force=True, ignore_missing=True, ignore_permissions=True
			)
		)

		delete_workspace(name=ws.name)

		self.assertFalse(frappe.db.exists("Raven Workspace Mapping", ws.name))
		self.assertTrue(
			frappe.db.exists("Raven Workspace", rw),
			"default delete must keep the Raven workspace",
		)


class TestChannelAPI(FrappeTestCase):
	def setUp(self):
		self.non_admin = frappe.get_doc(
			{
				"doctype": "User",
				"email": "rv-ch-non-admin@example.com",
				"first_name": "ChanPlain",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(self.non_admin.delete)

		# Create a workspace to use as a parent (skip_raven_create so no Raven call needed)
		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = "Ch API Test WS"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		self.workspace = ws.name
		self.addCleanup(lambda: frappe.delete_doc("Raven Workspace Mapping", self.workspace, force=True))

	def test_list_channels_requires_system_manager(self):
		from raven_integration.api import list_channels

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				list_channels(workspace=self.workspace)
		finally:
			frappe.set_user("Administrator")

	def test_create_channel_rejects_non_string_label(self):
		from raven_integration.api import create_channel

		with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
			create_channel(workspace=self.workspace, label=42, type="Private", rules=None)

	def test_create_channel_rejects_invalid_type(self):
		from raven_integration.api import create_channel

		with self.assertRaises(frappe.ValidationError):
			create_channel(
				workspace=self.workspace,
				label="Test Ch",
				type="weird",
				rules=None,
			)

	def test_create_channel_rejects_unknown_workspace(self):
		from raven_integration.api import create_channel

		with self.assertRaises(frappe.ValidationError):
			create_channel(
				workspace="RWM-does-not-exist",
				label="Test Ch",
				type="Private",
				rules=None,
			)

	def test_create_channel_persists_and_returns_name(self):
		from raven_integration.api import create_channel, create_workspace

		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		# Create a workspace with a real Raven link so the channel can be created under it.
		with patch.object(registry, "_provider_paths", return_value=[]):
			ws_name = create_workspace(label="Ch API Real WS", type="Private")
		self.addCleanup(lambda: frappe.delete_doc("Raven Workspace Mapping", ws_name, force=True))
		with patch.object(registry, "_provider_paths", return_value=[]):
			name = create_channel(
				workspace=ws_name,
				label="API Test Channel",
				type="Private",
				rules=None,
			)
		self.assertTrue(name.startswith("RCM-"))
		self.addCleanup(lambda: frappe.delete_doc("Raven Channel Mapping", name, force=True))

	def test_create_channel_auto_names_when_label_omitted(self):
		from raven_integration.api import create_channel, create_workspace

		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		with patch.object(registry, "_provider_paths", return_value=[]):
			ws_name = create_workspace(label="Ch AutoName WS", type="Private")
		self.addCleanup(lambda: frappe.delete_doc("Raven Workspace Mapping", ws_name, force=True))
		with patch.object(registry, "_provider_paths", return_value=[]):
			name = create_channel(workspace=ws_name)
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", name, force=True, ignore_missing=True)
		)
		label = frappe.db.get_value("Raven Channel Mapping", name, "channel_label")
		self.assertRegex(label, r"^Channel \d+$")


class TestReconcileNow(FrappeTestCase):
	"""reconcile_now endpoint — manual sync trigger."""

	def setUp(self):
		self.non_admin = frappe.get_doc(
			{
				"doctype": "User",
				"email": "rv-reconcile-non-admin@example.com",
				"first_name": "ReconcilePlain",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(self.non_admin.delete)

	def test_validates_target_doctype(self):
		from raven_integration.api import reconcile_now

		with self.assertRaises(frappe.ValidationError):
			reconcile_now(target_doctype="LMS Batch", name="some-name")

	def test_enqueues_sync_for_workspace(self):
		"""The member diff runs in the background, like the other sync triggers."""
		from raven_integration.api import reconcile_now

		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = "Reconcile Now Test WS"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		self.addCleanup(lambda: frappe.delete_doc("Raven Workspace Mapping", ws.name, force=True))

		with patch("raven_integration.api.frappe.enqueue") as enq:
			result = reconcile_now(target_doctype="Raven Workspace Mapping", name=ws.name)
		self.assertEqual(result, {"queued": True})
		enq.assert_called_once_with(
			"raven_integration.sync_service.sync_workspace_members",
			queue="short",
			enqueue_after_commit=True,
			workspace_name=ws.name,
		)

	def test_enqueues_sync_for_channel(self):
		from raven_integration.api import reconcile_now

		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = "Reconcile Now Test WS Parent"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", ws.name, force=True, ignore_missing=True)
		)

		ch = frappe.new_doc("Raven Channel Mapping")
		ch.channel_label = "Reconcile Now Test Channel"
		ch.workspace = ws.name
		ch.channel_type = "Private"
		ch.flags.skip_raven_create = True
		ch.insert()
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", ch.name, force=True, ignore_missing=True)
		)

		with patch("raven_integration.api.frappe.enqueue") as enq:
			result = reconcile_now(target_doctype="Raven Channel Mapping", name=ch.name)
		self.assertEqual(result, {"queued": True})
		enq.assert_called_once_with(
			"raven_integration.sync_service.sync_channel_members",
			queue="short",
			enqueue_after_commit=True,
			channel_name=ch.name,
		)

	def test_rejects_unknown_mapping_name(self):
		from raven_integration.api import reconcile_now

		with self.assertRaises(frappe.DoesNotExistError):
			reconcile_now(target_doctype="Raven Workspace Mapping", name="RWM-does-not-exist")

	def test_requires_system_manager(self):
		from raven_integration.api import reconcile_now

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				reconcile_now(target_doctype="Raven Workspace Mapping", name="anything")
		finally:
			frappe.set_user("Administrator")


class TestPreviewRule(FrappeTestCase):
	"""preview_rule endpoint — count + sample via FAKE provider."""

	def setUp(self):
		self.non_admin = frappe.get_doc(
			{
				"doctype": "User",
				"email": "rv-preview-non-admin@example.com",
				"first_name": "PreviewPlain",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(self.non_admin.delete)

	def test_returns_count_and_sample_via_fake(self):
		from raven_integration.api import preview_rule

		with patch.object(
			registry,
			"_provider_paths",
			return_value=["raven_integration.tests.fake_provider.get_provider"],
		):
			result = preview_rule({"provider": "FAKE", "rule_type": "always-ab", "config": {}})
		self.assertIn("matched_user_count", result)
		self.assertIn("sample_users", result)
		self.assertEqual(result["matched_user_count"], 2)
		self.assertIsInstance(result["sample_users"], list)
		self.assertLessEqual(len(result["sample_users"]), 5)
		for user in result["sample_users"]:
			self.assertIn(user, {"a@example.com", "b@example.com"})

	def test_rejects_non_dict_rule(self):
		from raven_integration.api import preview_rule

		with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
			preview_rule(rule="not-a-dict")

	def test_requires_system_manager(self):
		from raven_integration.api import preview_rule

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				preview_rule({"provider": "FAKE", "rule_type": "always-ab", "config": {}})
		finally:
			frappe.set_user("Administrator")


class TestComputeRuleDiff(FrappeTestCase):
	"""compute_rule_diff endpoint — diff old vs new rules server-side."""

	def setUp(self):
		self.non_admin = frappe.get_doc(
			{
				"doctype": "User",
				"email": "rv-diff-non-admin@example.com",
				"first_name": "DiffPlain",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(self.non_admin.delete)

		# Create a workspace with a FAKE always-ab rule (a@ and b@ match).
		with patch.object(
			registry,
			"_provider_paths",
			return_value=["raven_integration.tests.fake_provider.get_provider"],
		):
			ws = frappe.new_doc("Raven Workspace Mapping")
			ws.workspace_label = "Rule Diff Test WS"
			ws.workspace_type = "Private"
			ws.flags.skip_raven_create = True
			ws.insert()
			ch = frappe.new_doc("Raven Channel Mapping")
			ch.channel_label = "Rule Diff Test CH"
			ch.workspace = ws.name
			ch.channel_type = "Private"
			ch.flags.skip_raven_create = True
			ch.member_rules_json = frappe.as_json(
				{
					"conjunctions": [],
					"conditions": [
						{
							"label": "Rule 1",
							"provider": "FAKE",
							"rule_type": "always-ab",
							"status": "Active",
							"config": {},
						}
					],
				}
			)
			ch.insert()
		self.workspace_name = ws.name
		self.channel_name = ch.name
		self.addCleanup(lambda: frappe.delete_doc("Raven Workspace Mapping", self.workspace_name, force=True))
		self.addCleanup(lambda: frappe.delete_doc("Raven Channel Mapping", self.channel_name, force=True))

	def test_no_active_rules_reports_no_change(self):
		"""Must agree with sync, which skips a rule set with nothing active.

		test_sync_service.test_zero_rules_removes_nobody pins the sync side:
		`has_active_rules` short-circuits it, so nobody is removed. This used to
		report the whole population instead, which is what made the confirmation
		dialog announce it would drop every member of a workspace it would not
		actually touch.
		"""
		from raven_integration.api import compute_rule_diff

		with patch.object(
			registry,
			"_provider_paths",
			return_value=["raven_integration.tests.fake_provider.get_provider"],
		):
			result = compute_rule_diff(
				target_doctype="Raven Channel Mapping",
				name=self.channel_name,
				new_rules={"conjunctions": [], "conditions": []},
			)
		self.assertEqual(result, {"added": 0, "removed": 0, "removed_users": [], "unknown": False})

	def test_counts_against_actual_members_not_the_rule_population(self):
		"""Sync diffs against who is *actually* rule-managed, so the preview does too.

		Nothing has been synced into Raven here, so there is no one to remove — a
		set difference over the rules alone would claim otherwise.
		"""
		from raven_integration.api import compute_rule_diff

		with patch.object(
			registry,
			"_provider_paths",
			return_value=["raven_integration.tests.fake_provider.get_provider"],
		):
			result = compute_rule_diff(
				target_doctype="Raven Channel Mapping",
				name=self.channel_name,
				new_rules={
					"conjunctions": [],
					"conditions": [
						{"provider": "FAKE", "rule_type": "always-b", "config": {}, "status": "Active"}
					],
				},
			)
		self.assertEqual(result["removed"], 0)
		self.assertEqual(result["removed_users"], [])

	def test_defaults_to_the_saved_tree_when_none_is_proposed(self):
		"""Omitting new_rules previews the channel exactly as it stands."""
		from raven_integration.api import compute_rule_diff

		with patch.object(
			registry,
			"_provider_paths",
			return_value=["raven_integration.tests.fake_provider.get_provider"],
		):
			result = compute_rule_diff(
				target_doctype="Raven Channel Mapping",
				name=self.channel_name,
			)
		for key in ("added", "removed", "removed_users"):
			self.assertIn(key, result)

	def test_rejects_an_invalid_conjunction(self):
		from raven_integration.api import compute_rule_diff

		with self.assertRaises(frappe.ValidationError):
			compute_rule_diff(
				target_doctype="Raven Channel Mapping",
				name=self.channel_name,
				new_rules={"conjunctions": ["maybe"], "conditions": []},
			)

	def test_rejects_a_group_whose_joiners_do_not_match_its_conditions(self):
		from raven_integration.api import compute_rule_diff

		with self.assertRaises(frappe.ValidationError) as cm:
			compute_rule_diff(
				target_doctype="Raven Channel Mapping",
				name=self.channel_name,
				new_rules={
					"conjunctions": [],
					"conditions": [
						{"provider": "FAKE", "rule_type": "always-a", "config": {}, "status": "Active"},
						{"provider": "FAKE", "rule_type": "always-ab", "config": {}, "status": "Active"},
					],
				},
			)
		self.assertIn("joiners", str(cm.exception))

	def test_removed_users_capped_at_ten(self):
		from raven_integration.api import compute_rule_diff

		with patch.object(
			registry,
			"_provider_paths",
			return_value=["raven_integration.tests.fake_provider.get_provider"],
		):
			result = compute_rule_diff(
				target_doctype="Raven Channel Mapping",
				name=self.channel_name,
				new_rules={"conjunctions": [], "conditions": []},
			)
		self.assertLessEqual(len(result["removed_users"]), 10)

	def test_rejects_invalid_target_doctype(self):
		from raven_integration.api import compute_rule_diff

		with self.assertRaises(frappe.ValidationError):
			compute_rule_diff(
				target_doctype="LMS Batch",
				name=self.channel_name,
				new_rules=None,
			)

	def test_requires_system_manager(self):
		from raven_integration.api import compute_rule_diff

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				compute_rule_diff(
					target_doctype="Raven Channel Mapping",
					name=self.channel_name,
					new_rules=None,
				)
		finally:
			frappe.set_user("Administrator")


class TestMappingEnabledToggle(FrappeTestCase):
	"""set_channel_enabled — the per-channel disabled flag.

	A workspace mapping has no counterpart: its membership is derived from its
	channels, so switching those off is what stops it syncing.
	"""

	def setUp(self):
		self.non_admin = frappe.get_doc(
			{
				"doctype": "User",
				"email": "rv-toggle-non-admin@example.com",
				"first_name": "TogglePlain",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(self.non_admin.delete)

		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = "Toggle Test WS"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		self.workspace = ws.name
		self.addCleanup(lambda: frappe.delete_doc("Raven Workspace Mapping", self.workspace, force=True))

		ch = frappe.new_doc("Raven Channel Mapping")
		ch.channel_label = "Toggle Test Channel"
		ch.workspace = self.workspace
		ch.channel_type = "Private"
		ch.flags.skip_raven_create = True
		ch.insert()
		self.channel = ch.name
		self.addCleanup(lambda: frappe.delete_doc("Raven Channel Mapping", self.channel, force=True))

	def test_workspace_mapping_carries_no_enabled_field(self):
		# The endpoint is gone and so is the field it wrote. Pinned as a field-level
		# assertion rather than an import check so it fails if the column comes back
		# through the doctype JSON without an endpoint attached.
		from raven_integration import api

		self.assertNotIn("enabled", frappe.get_meta("Raven Workspace Mapping").get_valid_columns())
		self.assertFalse(hasattr(api, "set_workspace_enabled"))

	def test_set_channel_enabled_toggles_enabled_flag(self):
		from raven_integration.api import set_channel_enabled

		result = set_channel_enabled(name=self.channel, enabled=False)
		self.assertFalse(result["enabled"])
		self.assertEqual(frappe.db.get_value("Raven Channel Mapping", self.channel, "enabled"), 0)

		result = set_channel_enabled(name=self.channel, enabled=True)
		self.assertTrue(result["enabled"])
		self.assertEqual(frappe.db.get_value("Raven Channel Mapping", self.channel, "enabled"), 1)

	def test_set_channel_enabled_rejects_non_bool(self):
		from raven_integration.api import set_channel_enabled

		with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
			set_channel_enabled(name=self.channel, enabled="nope")

	def test_set_channel_enabled_rejects_non_string_name(self):
		from raven_integration.api import set_channel_enabled

		with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
			set_channel_enabled(name=42, enabled=True)

	def test_set_channel_enabled_requires_system_manager(self):
		from raven_integration.api import set_channel_enabled

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				set_channel_enabled(name=self.channel, enabled=True)
		finally:
			frappe.set_user("Administrator")


class TestMappingTypeChange(FrappeTestCase):
	"""set_workspace_type / set_channel_type — per-mapping visibility type field."""

	def setUp(self):
		self.non_admin = frappe.get_doc(
			{
				"doctype": "User",
				"email": "rv-type-non-admin@example.com",
				"first_name": "TypePlain",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(self.non_admin.delete)

		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = "Type Test WS"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		self.workspace = ws.name
		self.addCleanup(lambda: frappe.delete_doc("Raven Workspace Mapping", self.workspace, force=True))

		ch = frappe.new_doc("Raven Channel Mapping")
		ch.channel_label = "Type Test Channel"
		ch.workspace = self.workspace
		ch.channel_type = "Private"
		ch.flags.skip_raven_create = True
		ch.insert()
		self.channel = ch.name
		self.addCleanup(lambda: frappe.delete_doc("Raven Channel Mapping", self.channel, force=True))

	def test_set_workspace_type_changes_type(self):
		from raven_integration.api import set_workspace_type

		result = set_workspace_type(name=self.workspace, type="Public")
		self.assertEqual(result["type"], "Public")
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.workspace, "workspace_type"),
			"Public",
		)

	def test_set_channel_type_changes_type(self):
		from raven_integration.api import set_channel_type

		result = set_channel_type(name=self.channel, type="Open")
		self.assertEqual(result["type"], "Open")
		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.channel, "channel_type"),
			"Open",
		)

	def test_set_workspace_type_rejects_invalid_type(self):
		from raven_integration.api import set_workspace_type

		with self.assertRaises(frappe.ValidationError):
			set_workspace_type(name=self.workspace, type="Open")

	def test_set_channel_type_rejects_invalid_type(self):
		from raven_integration.api import set_channel_type

		with self.assertRaises(frappe.ValidationError):
			set_channel_type(name=self.channel, type="weird")

	def test_set_workspace_type_rejects_non_string(self):
		from raven_integration.api import set_workspace_type

		with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
			set_workspace_type(name=self.workspace, type=42)

	def test_set_channel_type_rejects_non_string(self):
		from raven_integration.api import set_channel_type

		with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
			set_channel_type(name=self.channel, type=42)

	def test_set_workspace_type_requires_system_manager(self):
		from raven_integration.api import set_workspace_type

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				set_workspace_type(name=self.workspace, type="Public")
		finally:
			frappe.set_user("Administrator")

	def test_set_channel_type_requires_system_manager(self):
		from raven_integration.api import set_channel_type

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				set_channel_type(name=self.channel, type="Public")
		finally:
			frappe.set_user("Administrator")


class TestMappingLabelChange(FrappeTestCase):
	"""set_workspace_label / set_channel_label — per-mapping free-text label rename."""

	def setUp(self):
		self.non_admin = frappe.get_doc(
			{
				"doctype": "User",
				"email": "rv-label-non-admin@example.com",
				"first_name": "LabelPlain",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(self.non_admin.delete)

		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = "Label Test WS"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		self.workspace = ws.name
		self.addCleanup(lambda: frappe.delete_doc("Raven Workspace Mapping", self.workspace, force=True))

		ch = frappe.new_doc("Raven Channel Mapping")
		ch.channel_label = "Label Test Channel"
		ch.workspace = self.workspace
		ch.channel_type = "Private"
		ch.flags.skip_raven_create = True
		ch.insert()
		self.channel = ch.name
		self.addCleanup(lambda: frappe.delete_doc("Raven Channel Mapping", self.channel, force=True))

	def test_set_workspace_label_changes_label_and_renames_doc(self):
		from raven_integration.api import set_workspace_label

		result = set_workspace_label(name=self.workspace, label="Renamed WS")
		self.workspace = result["name"]
		self.assertEqual(result["label"], "Renamed WS")
		# The docname is derived from the label (format:RWM-{workspace_label}), so
		# it must follow the label rather than drift away from it.
		self.assertEqual(result["name"], "RWM-Renamed WS")
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.workspace, "workspace_label"),
			"Renamed WS",
		)

	def test_set_workspace_label_repoints_child_channels(self):
		"""Renaming the workspace must not orphan the channels linked to it."""
		from raven_integration.api import set_workspace_label

		self.workspace = set_workspace_label(name=self.workspace, label="Relinked WS")["name"]
		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.channel, "workspace"),
			self.workspace,
		)

	def test_set_workspace_label_rejects_duplicate(self):
		from raven_integration.api import set_workspace_label

		other = frappe.new_doc("Raven Workspace Mapping")
		other.workspace_label = "Occupied Label WS"
		other.workspace_type = "Private"
		other.flags.skip_raven_create = True
		other.insert()
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", other.name, force=True, ignore_missing=True)
		)

		with self.assertRaises(frappe.ValidationError):
			set_workspace_label(name=self.workspace, label="Occupied Label WS")

	def test_set_channel_label_changes_label_and_renames_doc(self):
		from raven_integration.api import set_channel_label

		result = set_channel_label(name=self.channel, label="Renamed Channel")
		self.channel = result["name"]
		self.assertEqual(result["label"], "Renamed Channel")
		self.assertEqual(result["name"], "RCM-Renamed Channel")
		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.channel, "channel_label"),
			"Renamed Channel",
		)

	def test_set_workspace_label_rejects_unknown_name(self):
		from raven_integration.api import set_workspace_label

		with self.assertRaises(frappe.DoesNotExistError):
			set_workspace_label(name="RWM-does-not-exist", label="Nope")

	def test_set_channel_label_rejects_unknown_name(self):
		from raven_integration.api import set_channel_label

		with self.assertRaises(frappe.DoesNotExistError):
			set_channel_label(name="RCM-does-not-exist", label="Nope")

	def test_set_workspace_label_rejects_non_string(self):
		from raven_integration.api import set_workspace_label

		with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
			set_workspace_label(name=self.workspace, label=42)

	def test_set_channel_label_rejects_non_string(self):
		from raven_integration.api import set_channel_label

		with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
			set_channel_label(name=self.channel, label=42)

	def test_set_workspace_label_requires_system_manager(self):
		from raven_integration.api import set_workspace_label

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				set_workspace_label(name=self.workspace, label="Nope")
		finally:
			frappe.set_user("Administrator")

	def test_set_channel_label_requires_system_manager(self):
		from raven_integration.api import set_channel_label

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				set_channel_label(name=self.channel, label="Nope")
		finally:
			frappe.set_user("Administrator")


class TestDefaultLabelAllocation(FrappeTestCase):
	"""Regression: the auto-name probe must key off the docname, not the label field.

	Both mapping doctypes autoname as `format:<PREFIX>-{label}`. The label field
	carries no unique constraint, so probing it re-proposes docnames that are
	already taken and "Create workspace" throws forever."""

	def test_next_default_label_skips_names_taken_by_drifted_rows(self):
		from raven_integration.api import _next_default_label

		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = "Workspace 9001"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", ws.name, force=True, ignore_missing=True)
		)
		self.assertEqual(ws.name, "RWM-Workspace 9001")

		# Simulate a row written before labels renamed their doc: the docname still
		# holds "Workspace 9001" while the label field has moved on.
		frappe.db.set_value(
			"Raven Workspace Mapping", ws.name, "workspace_label", "Drifted", update_modified=False
		)

		self.assertNotEqual(
			_next_default_label("Raven Workspace Mapping", "Workspace"),
			"Workspace 9001",
			"probe re-proposed a label whose docname is already taken",
		)

	def test_create_workspace_after_inline_rename(self):
		"""Rename a workspace inline, then create a new one — the exact flow that
		used to deadlock on 'Could not allocate a default workspace name'."""
		from raven_integration.api import create_workspace, set_workspace_label

		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")

		created = []
		self.addCleanup(
			lambda: [
				frappe.delete_doc("Raven Workspace Mapping", n, force=True, ignore_missing=True)
				for n in created
			]
		)

		with patch.object(registry, "_provider_paths", return_value=[]):
			first = create_workspace()
			created.append(first)

			renamed = set_workspace_label(name=first, label="Renamed After Create WS")["name"]
			created[0] = renamed

			second = create_workspace()
			created.append(second)

		self.assertNotEqual(renamed, second)
		self.assertTrue(second.startswith("RWM-Workspace "))
		# The new record's docname and label agree — no drift left behind.
		self.assertEqual(
			second,
			"RWM-" + frappe.db.get_value("Raven Workspace Mapping", second, "workspace_label"),
		)

	def test_channel_rename_frees_its_default_docname(self):
		"""Same deadlock, channel side, asserted at the mapping layer.

		Not driven through create_channel end-to-end: that path also inserts a
		backing Raven Channel, and sync_service._unique_raven_name probes the raw
		label while Raven stores a slugified channel_name, so re-using a freed
		default label collides in Raven. That is a separate pre-existing defect in
		sync_service.py, reproducible without any rename."""
		from raven_integration.api import _set_mapping_label

		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = "Channel Rename Parent WS"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", ws.name, force=True, ignore_missing=True)
		)

		first = frappe.new_doc("Raven Channel Mapping")
		first.channel_label = "Channel 9001"
		first.workspace = ws.name
		first.channel_type = "Private"
		first.flags.skip_raven_create = True
		first.insert()
		self.assertEqual(first.name, "RCM-Channel 9001")

		renamed = _set_mapping_label("Raven Channel Mapping", first.name, "Renamed Channel Deadlock")
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", renamed, force=True, ignore_missing=True)
		)
		self.assertEqual(renamed, "RCM-Renamed Channel Deadlock")
		self.assertFalse(
			frappe.db.exists("Raven Channel Mapping", "RCM-Channel 9001"),
			"rename must free the old docname, not leave it pinned to the doc",
		)

		# The freed default name is now allocatable again — this is the insert that
		# used to raise DuplicateEntryError on every retry.
		second = frappe.new_doc("Raven Channel Mapping")
		second.channel_label = "Channel 9001"
		second.workspace = ws.name
		second.channel_type = "Private"
		second.flags.skip_raven_create = True
		second.insert()
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", second.name, force=True, ignore_missing=True)
		)
		self.assertEqual(second.name, "RCM-Channel 9001")


class TestMissingMappingFailsLoudly(FrappeTestCase):
	"""db.set_value on a missing row is a silent no-op — every set_* endpoint must
	reject an unknown name instead of reporting a change that never landed."""

	def test_set_workspace_type_rejects_unknown_name(self):
		from raven_integration.api import set_workspace_type

		with self.assertRaises(frappe.DoesNotExistError):
			set_workspace_type(name="RWM-does-not-exist", type="Public")

	def test_set_channel_enabled_rejects_unknown_name(self):
		from raven_integration.api import set_channel_enabled

		with self.assertRaises(frappe.DoesNotExistError):
			set_channel_enabled(name="RCM-does-not-exist", enabled=True)

	def test_set_channel_type_rejects_unknown_name(self):
		from raven_integration.api import set_channel_type

		with self.assertRaises(frappe.DoesNotExistError):
			set_channel_type(name="RCM-does-not-exist", type="Public")


class TestPreviewRuleValidatesConfig(FrappeTestCase):
	"""preview_rule must validate the config before handing it to the provider."""

	def _provider(self):
		return {
			"name": "REQCFG",
			"label": "Requires Config",
			"rule_types": [
				{
					"type": "needs-batch",
					"label": "Needs Batch",
					"fields": [{"fieldname": "batch", "label": "Batch", "reqd": 1}],
				}
			],
			"evaluate": lambda rule_type, config: {config["batch"]},
			"triggers": [],
		}

	def test_rejects_config_missing_required_field(self):
		from raven_integration.api import preview_rule

		with patch.object(registry, "_load_providers", return_value={"REQCFG": self._provider()}):
			with self.assertRaises(frappe.ValidationError):
				preview_rule({"provider": "REQCFG", "rule_type": "needs-batch", "config": {}})

	def test_accepts_valid_config(self):
		from raven_integration.api import preview_rule

		with patch.object(registry, "_load_providers", return_value={"REQCFG": self._provider()}):
			result = preview_rule(
				{"provider": "REQCFG", "rule_type": "needs-batch", "config": {"batch": "a@x.com"}}
			)
		self.assertEqual(result["matched_user_count"], 1)


_FAKE = ["raven_integration.tests.fake_provider.get_provider"]


def _leaf(label, rule_type="always-ab", config=None):
	return {
		"label": label,
		"provider": "FAKE",
		"rule_type": rule_type,
		"status": "Active",
		"config": config if config is not None else {},
	}


class TestUpdateChannelRules(FrappeTestCase):
	"""update_channel carries the whole settings form, so what it does with a
	`rules` the caller did not send decides whether a rename can delete a channel's
	conditions."""

	def setUp(self):
		self.tree = {
			"conjunctions": ["or"],
			"conditions": [_leaf("First Rule"), _leaf("Second Rule", config={"tag": "b"})],
		}
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			ws = frappe.new_doc("Raven Workspace Mapping")
			ws.workspace_label = f"Upd Rules WS {frappe.generate_hash(length=6)}"
			ws.workspace_type = "Private"
			ws.flags.skip_raven_create = True
			ws.insert()
			ch = frappe.new_doc("Raven Channel Mapping")
			ch.channel_label = f"Upd Rules CH {frappe.generate_hash(length=6)}"
			ch.workspace = ws.name
			ch.channel_type = "Private"
			ch.flags.skip_raven_create = True
			ch.member_rules_json = frappe.as_json(self.tree)
			ch.insert()
		self.workspace = ws.name
		self.channel = ch.name
		self.addCleanup(
			lambda: frappe.delete_doc(
				"Raven Workspace Mapping", self.workspace, force=True, ignore_missing=True
			)
		)
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", self.channel, force=True, ignore_missing=True)
		)

	def _stored(self):
		return frappe.parse_json(
			frappe.db.get_value("Raven Channel Mapping", self.channel, "member_rules_json")
		)

	def test_a_rename_that_sends_no_rules_keeps_the_conditions(self):
		"""Omitting `rules` means "I am not editing the conditions". Writing an empty
		tree for it deletes every condition on the channel and evicts the members they
		granted, for a caller that only changed the name."""
		from raven_integration.api import update_channel

		label = frappe.db.get_value("Raven Channel Mapping", self.channel, "channel_label")
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			self.channel = update_channel(name=self.channel, label=f"{label} Renamed", type="Public")

		self.assertEqual(self._stored(), self.tree)
		self.assertEqual(frappe.db.get_value("Raven Channel Mapping", self.channel, "channel_type"), "Public")

	def test_a_save_that_sends_no_rules_does_not_schedule_a_resync(self):
		"""No condition changed, so nothing can have moved a member. Resyncing anyway
		is what turns the silent tree wipe into an eviction."""
		from raven_integration import api

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with patch.object(api, "_schedule_resync") as scheduled:
				label = frappe.db.get_value("Raven Channel Mapping", self.channel, "channel_label")
				api.update_channel(name=self.channel, label=label, type="Public")
		scheduled.assert_not_called()

	def test_an_explicitly_empty_tree_still_clears_the_conditions(self):
		"""The other half of the contract: "delete every condition" is a thing the UI
		must still be able to say, and it says it by sending an empty group."""
		from raven_integration import api

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with patch.object(api, "_schedule_resync") as scheduled:
				label = frappe.db.get_value("Raven Channel Mapping", self.channel, "channel_label")
				api.update_channel(
					name=self.channel,
					label=label,
					type="Private",
					rules={"conjunctions": [], "conditions": []},
				)
		self.assertEqual(self._stored(), {"conjunctions": [], "conditions": []})
		scheduled.assert_called_once()

	def test_new_conditions_replace_the_stored_ones(self):
		from raven_integration import api

		replacement = {"conjunctions": [], "conditions": [_leaf("Only Rule")]}
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with patch.object(api, "_schedule_resync") as scheduled:
				label = frappe.db.get_value("Raven Channel Mapping", self.channel, "channel_label")
				api.update_channel(name=self.channel, label=label, type="Private", rules=replacement)
		self.assertEqual([c["label"] for c in self._stored()["conditions"]], ["Only Rule"])
		scheduled.assert_called_once()


class TestSerializeTreeForUI(FrappeTestCase):
	"""What the rules panel is handed has to be a tree it can send straight back:
	one joiner per gap between conditions, which is what the save path enforces."""

	def setUp(self):
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			ws = frappe.new_doc("Raven Workspace Mapping")
			ws.workspace_label = f"Serialize WS {frappe.generate_hash(length=6)}"
			ws.workspace_type = "Private"
			ws.flags.skip_raven_create = True
			ws.insert()
			ch = frappe.new_doc("Raven Channel Mapping")
			ch.channel_label = f"Serialize CH {frappe.generate_hash(length=6)}"
			ch.workspace = ws.name
			ch.channel_type = "Private"
			ch.flags.skip_raven_create = True
			ch.member_rules_json = frappe.as_json(
				{
					"conjunctions": ["or"],
					"conditions": [_leaf("First Rule"), _leaf("Second", config={"t": 1})],
				}
			)
			ch.insert()
		self.workspace = ws.name
		self.channel = ch.name
		self.addCleanup(
			lambda: frappe.delete_doc(
				"Raven Workspace Mapping", self.workspace, force=True, ignore_missing=True
			)
		)
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", self.channel, force=True, ignore_missing=True)
		)

	def _store(self, tree):
		# db.set_value, not the doctype: this is a payload validation would reject,
		# which is the point — it is what a hand-edited JSON field can leave behind.
		frappe.db.set_value("Raven Channel Mapping", self.channel, "member_rules_json", frappe.as_json(tree))

	def test_a_broken_child_takes_its_joiner_with_it(self):
		from raven_integration.api import _serialize_tree_for_ui

		self._store(
			{
				"conjunctions": ["or", "and"],
				"conditions": [_leaf("Keep"), None, _leaf("Keep Too", config={"t": 2})],
			}
		)
		served = _serialize_tree_for_ui(
			frappe.db.get_value("Raven Channel Mapping", self.channel, "member_rules_json")
		)
		self.assertEqual([c["label"] for c in served["conditions"]], ["Keep", "Keep Too"])
		self.assertEqual(len(served["conjunctions"]), len(served["conditions"]) - 1)

	def test_what_the_panel_is_served_can_be_saved_back(self):
		"""The consequence of a joiner count that does not match: the panel posts the
		tree back untouched and the save is refused with "Reload the page and try again",
		which reloading cannot fix."""
		from raven_integration.api import get_channel, update_channel

		self._store(
			{
				"conjunctions": ["or", "and"],
				"conditions": [_leaf("Keep"), None, _leaf("Keep Too", config={"t": 2})],
			}
		)
		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			served = get_channel(self.channel)["rules"]
			label = frappe.db.get_value("Raven Channel Mapping", self.channel, "channel_label")
			update_channel(name=self.channel, label=label, type="Private", rules=served)

		stored = frappe.parse_json(
			frappe.db.get_value("Raven Channel Mapping", self.channel, "member_rules_json")
		)
		self.assertEqual([c["label"] for c in stored["conditions"]], ["Keep", "Keep Too"])


class TestLabelPropagatesToRaven(FrappeTestCase):
	"""set_workspace_label / set_channel_label must propagate the rename to the
	backing Raven records, not only the mapping. Needs a real Raven workspace +
	channel, adopted by mappings via skip_raven_create."""

	def setUp(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		self._suffix = frappe.generate_hash(length=6)
		# The workspace rename changes the Raven Workspace's name, so track every
		# name it may end up under and clean them all.
		self._raven_ws_names = set()
		self._raven_ch_names = set()

		rw = frappe.get_doc(
			{"doctype": "Raven Workspace", "workspace_name": f"RI Lbl WS {self._suffix}", "type": "Private"}
		).insert(ignore_permissions=True)
		self.raven_ws = rw.name
		self._raven_ws_names.add(rw.name)

		rc = frappe.get_doc(
			{
				"doctype": "Raven Channel",
				"channel_name": f"ri-lbl-ch-{self._suffix}",
				"workspace": rw.name,
				"type": "Private",
			}
		).insert(ignore_permissions=True)
		self.raven_ch = rc.name
		self._raven_ch_names.add(rc.name)

		ws_map = frappe.new_doc("Raven Workspace Mapping")
		ws_map.workspace_label = self.raven_ws
		ws_map.workspace_type = "Private"
		ws_map.flags.skip_raven_create = True
		ws_map.insert()
		frappe.db.set_value("Raven Workspace Mapping", ws_map.name, "raven_workspace", self.raven_ws)
		self.ws_map = ws_map.name

		ch_map = frappe.new_doc("Raven Channel Mapping")
		ch_map.channel_label = f"ri-lbl-ch-{self._suffix}"
		ch_map.workspace = self.ws_map
		ch_map.channel_type = "Private"
		ch_map.flags.skip_raven_create = True
		ch_map.insert()
		frappe.db.set_value("Raven Channel Mapping", ch_map.name, "raven_channel", self.raven_ch)
		self.ch_map = ch_map.name

		self.addCleanup(self._cleanup)

	def _cleanup(self):
		frappe.set_user("Administrator")
		if frappe.db.exists("Raven Channel Mapping", self.ch_map):
			frappe.delete_doc(
				"Raven Channel Mapping", self.ch_map, force=True, ignore_permissions=True, ignore_missing=True
			)
		if frappe.db.exists("Raven Workspace Mapping", self.ws_map):
			frappe.delete_doc(
				"Raven Workspace Mapping",
				self.ws_map,
				force=True,
				ignore_permissions=True,
				ignore_missing=True,
			)
		# frappe.db.delete bypasses Raven's on_trash guards during teardown.
		for n in self._raven_ch_names:
			frappe.db.delete("Raven Channel Member", {"channel_id": n})
			frappe.db.delete("Raven Message", {"channel_id": n})
			frappe.db.delete("Raven Channel", {"name": n})
		for n in self._raven_ws_names:
			frappe.db.delete("Raven Workspace Member", {"workspace": n})
			frappe.db.delete("Raven Workspace", {"name": n})

	def test_workspace_rename_propagates_to_raven(self):
		from raven_integration.api import set_workspace_label

		new_label = f"RI Lbl WS Renamed {self._suffix}"
		result = set_workspace_label(name=self.ws_map, label=new_label)
		self.ws_map = result["name"]
		self._raven_ws_names.add(new_label)

		# The Raven Workspace's name IS its workspace_name, so the rename moves it.
		self.assertTrue(frappe.db.exists("Raven Workspace", new_label))
		self.assertFalse(frappe.db.exists("Raven Workspace", self.raven_ws))
		# The mapping's raven_workspace Link followed the rename (rename_doc cascade).
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.ws_map, "raven_workspace"), new_label
		)
		self.raven_ws = new_label

	def test_channel_rename_propagates_to_raven(self):
		from raven_integration.api import set_channel_label

		new_label = f"RI Lbl Ch Renamed {self._suffix}"
		result = set_channel_label(name=self.ch_map, label=new_label)
		self.ch_map = result["name"]

		# The channel id is unchanged; only channel_name is updated (Raven slugifies).
		self.assertTrue(frappe.db.exists("Raven Channel", self.raven_ch))
		self.assertEqual(
			frappe.db.get_value("Raven Channel", self.raven_ch, "channel_name"),
			new_label.strip().lower().replace(" ", "-"),
		)
		# The mapping's raven_channel Link stays valid.
		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.ch_map, "raven_channel"), self.raven_ch
		)

	def test_update_workspace_rename_propagates_to_raven(self):
		"""The settings page commits the workspace's name and visibility through
		update_workspace, so a rename travelling that path must reach Raven exactly as
		set_workspace_label does. A Raven Workspace's docname IS its display name and
		nothing reverse-syncs, so a rename that stops at the mapping leaves the two
		names apart for good."""
		from raven_integration.api import update_workspace

		new_label = f"RI Upd WS Renamed {self._suffix}"
		self._raven_ws_names.add(new_label)
		self.ws_map = update_workspace(name=self.ws_map, label=new_label, type="Public")

		self.assertTrue(frappe.db.exists("Raven Workspace", new_label))
		self.assertFalse(frappe.db.exists("Raven Workspace", self.raven_ws))
		# The mapping's raven_workspace Link followed the rename (rename_doc cascade).
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.ws_map, "raven_workspace"), new_label
		)
		self.raven_ws = new_label

	def test_update_workspace_without_a_rename_leaves_the_raven_name_alone(self):
		from raven_integration.api import update_workspace

		stored = frappe.db.get_value("Raven Workspace Mapping", self.ws_map, "workspace_label")
		self.ws_map = update_workspace(name=self.ws_map, label=stored, type="Private")
		self.assertTrue(frappe.db.exists("Raven Workspace", self.raven_ws))

	def test_update_workspace_failed_rename_rolls_the_raven_write_back(self):
		"""The Raven rename lands before the mapping's, so a mapping-side failure has
		to undo it: two mappings cannot share a docname."""
		from raven_integration.api import update_workspace

		taken = frappe.new_doc("Raven Workspace Mapping")
		taken.workspace_label = f"RI WS Taken {self._suffix}"
		taken.workspace_type = "Private"
		taken.flags.skip_raven_create = True
		taken.insert()
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", taken.name, force=True, ignore_missing=True)
		)
		self._raven_ws_names.add(f"RI WS Taken {self._suffix}")

		with self.assertRaises(frappe.ValidationError):
			update_workspace(name=self.ws_map, label=f"RI WS Taken {self._suffix}", type="Private")

		# Nothing half-renamed: the Raven Workspace still stands under its own name.
		self.assertTrue(frappe.db.exists("Raven Workspace", self.raven_ws))
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.ws_map, "raven_workspace"), self.raven_ws
		)

	def test_update_channel_rename_propagates_to_raven(self):
		"""The settings page commits name, visibility and conditions through
		update_channel, so a rename travelling that path must reach Raven exactly
		as set_channel_label does. It did not: _update_mapping renamed only the
		mapping, and the two names diverged with nothing to say so."""
		from raven_integration.api import update_channel

		new_label = f"RI Upd Ch Renamed {self._suffix}"
		self.ch_map = update_channel(name=self.ch_map, label=new_label, type="Public", rules=None)

		self.assertEqual(self.ch_map, f"RCM-{new_label}")
		self.assertEqual(
			frappe.db.get_value("Raven Channel", self.raven_ch, "channel_name"),
			new_label.strip().lower().replace(" ", "-"),
		)
		# Everything else the one call carries landed too, and the link is intact.
		self.assertEqual(frappe.db.get_value("Raven Channel Mapping", self.ch_map, "channel_type"), "Public")
		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.ch_map, "raven_channel"), self.raven_ch
		)

	def test_update_channel_without_a_rename_leaves_the_raven_name_alone(self):
		"""Only a *changed* label is propagated. Re-saving the same name must leave
		the Raven channel_name exactly as it was, slug and all."""
		from raven_integration.api import update_channel

		stored = frappe.db.get_value("Raven Channel Mapping", self.ch_map, "channel_label")
		before = frappe.db.get_value("Raven Channel", self.raven_ch, "channel_name")
		self.ch_map = update_channel(name=self.ch_map, label=stored, type="Public", rules=None)
		self.assertEqual(frappe.db.get_value("Raven Channel", self.raven_ch, "channel_name"), before)

	def test_update_channel_failed_rename_rolls_the_raven_write_back(self):
		"""The Raven write happens before the mapping rename, so a mapping-side
		failure has to undo it. Raven itself will not raise here — its duplicate
		guard only runs on insert — so the collision is forced where it really
		lives: two mappings cannot share a docname."""
		from raven_integration.api import update_channel

		taken = frappe.new_doc("Raven Channel Mapping")
		taken.channel_label = f"RI Taken {self._suffix}"
		taken.workspace = self.ws_map
		taken.channel_type = "Private"
		taken.flags.skip_raven_create = True
		taken.insert()
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", taken.name, force=True, ignore_missing=True)
		)

		before_name = frappe.db.get_value("Raven Channel", self.raven_ch, "channel_name")
		before_type = frappe.db.get_value("Raven Channel Mapping", self.ch_map, "channel_type")

		with self.assertRaises(frappe.ValidationError):
			update_channel(name=self.ch_map, label=f"RI Taken {self._suffix}", type="Public", rules=None)

		# Nothing half-applied: the Raven name, the mapping and its visibility all
		# stand where they were.
		self.assertEqual(frappe.db.get_value("Raven Channel", self.raven_ch, "channel_name"), before_name)
		self.assertTrue(frappe.db.exists("Raven Channel Mapping", self.ch_map))
		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.ch_map, "channel_type"), before_type
		)

	def test_stale_mapping_skips_raven_rename(self):
		from raven_integration.api import set_workspace_label

		frappe.db.set_value("Raven Workspace Mapping", self.ws_map, "stale", 1)
		new_label = f"RI Lbl WS Stale {self._suffix}"
		result = set_workspace_label(name=self.ws_map, label=new_label)
		self.ws_map = result["name"]

		# Stale → the Raven Workspace is left untouched under its old name.
		self.assertTrue(frappe.db.exists("Raven Workspace", self.raven_ws))
		self.assertFalse(frappe.db.exists("Raven Workspace", new_label))
		# The mapping label still changed.
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.ws_map, "workspace_label"), new_label
		)

	def test_workspace_rename_collision_throws_and_rolls_back(self):
		from raven_integration.api import set_workspace_label

		occupied = frappe.get_doc(
			{
				"doctype": "Raven Workspace",
				"workspace_name": f"RI Lbl Occupied {self._suffix}",
				"type": "Private",
			}
		).insert(ignore_permissions=True)
		self._raven_ws_names.add(occupied.name)

		with self.assertRaises(frappe.ValidationError):
			set_workspace_label(name=self.ws_map, label=occupied.name)

		# Nothing half-renamed: original Raven Workspace + mapping link intact.
		self.assertTrue(frappe.db.exists("Raven Workspace", self.raven_ws))
		self.assertTrue(frappe.db.exists("Raven Workspace Mapping", self.ws_map))
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.ws_map, "raven_workspace"), self.raven_ws
		)


class TestTypePropagatesToRaven(FrappeTestCase):
	"""set_workspace_type / set_channel_type / update_workspace must propagate a
	visibility change to the backing Raven records, not only the mapping. Needs a
	real Raven workspace + channel, adopted by mappings via skip_raven_create."""

	def setUp(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		self._suffix = frappe.generate_hash(length=6)

		rw = frappe.get_doc(
			{"doctype": "Raven Workspace", "workspace_name": f"RI Type WS {self._suffix}", "type": "Private"}
		).insert(ignore_permissions=True)
		self.raven_ws = rw.name

		rc = frappe.get_doc(
			{
				"doctype": "Raven Channel",
				"channel_name": f"ri-type-ch-{self._suffix}",
				"workspace": rw.name,
				"type": "Private",
			}
		).insert(ignore_permissions=True)
		self.raven_ch = rc.name

		ws_map = frappe.new_doc("Raven Workspace Mapping")
		ws_map.workspace_label = self.raven_ws
		ws_map.workspace_type = "Private"
		ws_map.flags.skip_raven_create = True
		ws_map.insert()
		frappe.db.set_value("Raven Workspace Mapping", ws_map.name, "raven_workspace", self.raven_ws)
		self.ws_map = ws_map.name

		ch_map = frappe.new_doc("Raven Channel Mapping")
		ch_map.channel_label = f"ri-type-ch-{self._suffix}"
		ch_map.workspace = self.ws_map
		ch_map.channel_type = "Private"
		ch_map.flags.skip_raven_create = True
		ch_map.insert()
		frappe.db.set_value("Raven Channel Mapping", ch_map.name, "raven_channel", self.raven_ch)
		self.ch_map = ch_map.name

		self.addCleanup(self._cleanup)

	def _cleanup(self):
		frappe.set_user("Administrator")
		if frappe.db.exists("Raven Channel Mapping", self.ch_map):
			frappe.delete_doc(
				"Raven Channel Mapping", self.ch_map, force=True, ignore_permissions=True, ignore_missing=True
			)
		if frappe.db.exists("Raven Workspace Mapping", self.ws_map):
			frappe.delete_doc(
				"Raven Workspace Mapping",
				self.ws_map,
				force=True,
				ignore_permissions=True,
				ignore_missing=True,
			)
		# frappe.db.delete bypasses Raven's on_trash guards during teardown.
		frappe.db.delete("Raven Channel Member", {"channel_id": self.raven_ch})
		frappe.db.delete("Raven Message", {"channel_id": self.raven_ch})
		frappe.db.delete("Raven Channel", {"name": self.raven_ch})
		frappe.db.delete("Raven Workspace Member", {"workspace": self.raven_ws})
		frappe.db.delete("Raven Workspace", {"name": self.raven_ws})

	def test_set_workspace_type_propagates_to_raven(self):
		from raven_integration.api import set_workspace_type

		result = set_workspace_type(name=self.ws_map, type="Public")

		self.assertEqual(result["type"], "Public")
		# The mapping records the new type...
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.ws_map, "workspace_type"), "Public"
		)
		# ...and, crucially, so does the backing Raven Workspace.
		self.assertEqual(frappe.db.get_value("Raven Workspace", self.raven_ws, "type"), "Public")

	def test_set_channel_type_propagates_to_raven(self):
		from raven_integration.api import set_channel_type

		result = set_channel_type(name=self.ch_map, type="Public")

		self.assertEqual(result["type"], "Public")
		self.assertEqual(frappe.db.get_value("Raven Channel Mapping", self.ch_map, "channel_type"), "Public")
		self.assertEqual(frappe.db.get_value("Raven Channel", self.raven_ch, "type"), "Public")

	def test_update_workspace_propagates_type_to_raven(self):
		"""The full update path (not just the inline setter) must propagate too."""
		from raven_integration.api import update_workspace

		update_workspace(name=self.ws_map, label=self.raven_ws, type="Public")

		self.assertEqual(frappe.db.get_value("Raven Workspace", self.raven_ws, "type"), "Public")

	def test_stale_mapping_skips_raven_type_change(self):
		"""A stale mapping (its Raven record was deleted) records the new type on the
		mapping but must not touch any Raven record."""
		from raven_integration.api import set_workspace_type

		frappe.db.set_value("Raven Workspace Mapping", self.ws_map, "stale", 1)
		set_workspace_type(name=self.ws_map, type="Public")

		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.ws_map, "workspace_type"), "Public"
		)
		# The Raven Workspace keeps its original visibility.
		self.assertEqual(frappe.db.get_value("Raven Workspace", self.raven_ws, "type"), "Private")


class TestRavenWorkspaceChangesSyncBack(FrappeTestCase):
	"""A change made to a Raven Workspace *inside Raven* (its name or visibility)
	must mirror back onto the mapping, and must not bounce back out as a forward
	push (loop). Needs a real Raven workspace adopted by a mapping."""

	def setUp(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		self._suffix = frappe.generate_hash(length=6)
		# A rename changes the Raven Workspace's name, so track every name it may end
		# up under and clean them all.
		self._raven_ws_names = set()

		rw = frappe.get_doc(
			{"doctype": "Raven Workspace", "workspace_name": f"RI Back WS {self._suffix}", "type": "Private"}
		).insert(ignore_permissions=True)
		self.raven_ws = rw.name
		self._raven_ws_names.add(rw.name)

		ws_map = frappe.new_doc("Raven Workspace Mapping")
		ws_map.workspace_label = self.raven_ws
		ws_map.workspace_type = "Private"
		ws_map.flags.skip_raven_create = True
		ws_map.insert()
		frappe.db.set_value("Raven Workspace Mapping", ws_map.name, "raven_workspace", self.raven_ws)
		self.ws_map = ws_map.name

		self.addCleanup(self._cleanup)

	def _cleanup(self):
		frappe.set_user("Administrator")
		# The mapping may have been renamed by the reverse sync; find it by link.
		for name in frappe.get_all(
			"Raven Workspace Mapping",
			filters={"raven_workspace": ["in", list(self._raven_ws_names)]},
			pluck="name",
		) + ([self.ws_map] if frappe.db.exists("Raven Workspace Mapping", self.ws_map) else []):
			frappe.delete_doc(
				"Raven Workspace Mapping", name, force=True, ignore_permissions=True, ignore_missing=True
			)
		for n in self._raven_ws_names:
			frappe.db.delete("Raven Workspace Member", {"workspace": n})
			frappe.db.delete("Raven Workspace", {"name": n})

	def test_raven_type_change_syncs_to_mapping(self):
		"""Flipping the workspace's visibility inside Raven updates the mapping."""
		rw = frappe.get_doc("Raven Workspace", self.raven_ws)
		rw.type = "Public"
		rw.save(ignore_permissions=True)

		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.ws_map, "workspace_type"), "Public"
		)

	def test_raven_rename_syncs_to_mapping(self):
		"""Renaming the workspace inside Raven updates the mapping's label, its own
		docname, and leaves the raven_workspace link pointing at the new name."""
		new_name = f"RI Back WS Renamed {self._suffix}"
		frappe.rename_doc("Raven Workspace", self.raven_ws, new_name, force=True)
		self._raven_ws_names.add(new_name)

		# The mapping followed the rename: link, label, and its own docname.
		mapping = frappe.db.get_value(
			"Raven Workspace Mapping",
			{"raven_workspace": new_name},
			["name", "workspace_label"],
			as_dict=True,
		)
		self.assertIsNotNone(mapping)
		self.assertEqual(mapping.workspace_label, new_name)
		self.assertEqual(mapping.name, f"RWM-{new_name}")
		self.ws_map = mapping.name

	def test_forward_label_rename_still_works(self):
		"""Regression: the reverse after_rename handler must be suppressed while the
		forward set_workspace_label endpoint renames Raven, or the endpoint would
		rename the mapping twice and fail on the second pass."""
		from raven_integration.api import set_workspace_label

		new_label = f"RI Back WS Fwd {self._suffix}"
		result = set_workspace_label(name=self.ws_map, label=new_label)
		self._raven_ws_names.add(new_label)
		self.ws_map = result["name"]

		self.assertTrue(frappe.db.exists("Raven Workspace", new_label))
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.ws_map, "workspace_label"), new_label
		)
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.ws_map, "raven_workspace"), new_label
		)


class TestLinkExisting(FrappeTestCase):
	"""list_unmapped_* / link_* — adopt Raven workspaces & channels created outside
	the integration, without creating any new Raven record."""

	def setUp(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		self._suffix = frappe.generate_hash(length=6)
		self._raven_ws_names = set()
		self._raven_ch_names = set()
		self._mappings: list[tuple[str, str]] = []

		self.non_admin = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"rv-link-{self._suffix}@example.com",
				"first_name": "LinkPlain",
				"send_welcome_email": 0,
			}
		).insert()
		self.addCleanup(self.non_admin.delete)

		rw = frappe.get_doc(
			{"doctype": "Raven Workspace", "workspace_name": f"RI Link WS {self._suffix}", "type": "Public"}
		).insert(ignore_permissions=True)
		self.raven_ws = rw.name
		self._raven_ws_names.add(rw.name)

		rc = frappe.get_doc(
			{
				"doctype": "Raven Channel",
				"channel_name": f"ri-link-ch-{self._suffix}",
				"workspace": rw.name,
				"type": "Public",
			}
		).insert(ignore_permissions=True)
		self.raven_ch = rc.name
		self._raven_ch_names.add(rc.name)

		self.addCleanup(self._cleanup)

	def _cleanup(self):
		frappe.set_user("Administrator")
		for dt, n in reversed(self._mappings):
			if frappe.db.exists(dt, n):
				frappe.delete_doc(dt, n, force=True, ignore_permissions=True, ignore_missing=True)
		for n in self._raven_ch_names:
			frappe.db.delete("Raven Channel Member", {"channel_id": n})
			frappe.db.delete("Raven Channel", {"name": n})
		for n in self._raven_ws_names:
			frappe.db.delete("Raven Workspace Member", {"workspace": n})
			frappe.db.delete("Raven Workspace", {"name": n})

	def _track(self, doctype: str, name: str) -> str:
		self._mappings.append((doctype, name))
		return name

	# --- list_unmapped_workspaces / link_workspace ---

	def test_list_unmapped_workspaces_excludes_mapped(self):
		from raven_integration.api import link_workspace, list_unmapped_workspaces

		rows = list_unmapped_workspaces()
		row = next((w for w in rows if w["name"] == self.raven_ws), None)
		self.assertIsNotNone(row, "a freshly created Raven Workspace must be unmapped")
		self.assertIn("workspace_name", row)
		self.assertIn("type", row)

		self._track("Raven Workspace Mapping", link_workspace(raven_workspace=self.raven_ws))
		after = [w["name"] for w in list_unmapped_workspaces()]
		self.assertNotIn(self.raven_ws, after, "a linked workspace must drop off the unmapped list")

	def test_link_workspace_adopts_without_creating_raven(self):
		from raven_integration.api import link_workspace

		before = frappe.db.count("Raven Workspace")
		name = self._track("Raven Workspace Mapping", link_workspace(raven_workspace=self.raven_ws))
		self.assertEqual(
			frappe.db.count("Raven Workspace"), before, "linking must not create a Raven Workspace"
		)
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", name, "raven_workspace"), self.raven_ws
		)
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", name, "workspace_label"), self.raven_ws
		)
		self.assertEqual(frappe.db.get_value("Raven Workspace Mapping", name, "workspace_type"), "Public")

	def test_link_workspace_rejects_already_mapped(self):
		from raven_integration.api import link_workspace

		self._track("Raven Workspace Mapping", link_workspace(raven_workspace=self.raven_ws))
		with self.assertRaises(frappe.ValidationError):
			link_workspace(raven_workspace=self.raven_ws)

	def test_link_workspace_rejects_missing_id(self):
		from raven_integration.api import link_workspace

		with self.assertRaises(frappe.DoesNotExistError):
			link_workspace(raven_workspace="RW-does-not-exist")

	def test_link_workspace_requires_system_manager(self):
		from raven_integration.api import link_workspace

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				link_workspace(raven_workspace=self.raven_ws)
		finally:
			frappe.set_user("Administrator")

	def test_list_unmapped_workspaces_requires_system_manager(self):
		from raven_integration.api import list_unmapped_workspaces

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				list_unmapped_workspaces()
		finally:
			frappe.set_user("Administrator")

	# --- list_unmapped_channels / link_channel ---

	def test_list_unmapped_channels_excludes_mapped(self):
		from raven_integration.api import link_channel, link_workspace, list_unmapped_channels

		ws_map = self._track("Raven Workspace Mapping", link_workspace(raven_workspace=self.raven_ws))
		rows = list_unmapped_channels(workspace=ws_map)
		row = next((c for c in rows if c["name"] == self.raven_ch), None)
		self.assertIsNotNone(row, "a channel in the workspace must be unmapped")
		self.assertIn("channel_name", row)
		self.assertIn("type", row)

		self._track("Raven Channel Mapping", link_channel(workspace=ws_map, raven_channel=self.raven_ch))
		after = [c["name"] for c in list_unmapped_channels(workspace=ws_map)]
		self.assertNotIn(self.raven_ch, after, "a linked channel must drop off the unmapped list")

	def test_link_channel_adopts_without_creating_raven(self):
		from raven_integration.api import link_channel, link_workspace

		ws_map = self._track("Raven Workspace Mapping", link_workspace(raven_workspace=self.raven_ws))
		before = frappe.db.count("Raven Channel")
		name = self._track(
			"Raven Channel Mapping", link_channel(workspace=ws_map, raven_channel=self.raven_ch)
		)
		self.assertEqual(frappe.db.count("Raven Channel"), before, "linking must not create a Raven Channel")
		self.assertEqual(frappe.db.get_value("Raven Channel Mapping", name, "raven_channel"), self.raven_ch)
		self.assertEqual(frappe.db.get_value("Raven Channel Mapping", name, "workspace"), ws_map)

	def test_link_channel_rejects_already_mapped(self):
		from raven_integration.api import link_channel, link_workspace

		ws_map = self._track("Raven Workspace Mapping", link_workspace(raven_workspace=self.raven_ws))
		self._track("Raven Channel Mapping", link_channel(workspace=ws_map, raven_channel=self.raven_ch))
		with self.assertRaises(frappe.ValidationError):
			link_channel(workspace=ws_map, raven_channel=self.raven_ch)

	def test_link_channel_rejects_missing_id(self):
		from raven_integration.api import link_channel, link_workspace

		ws_map = self._track("Raven Workspace Mapping", link_workspace(raven_workspace=self.raven_ws))
		with self.assertRaises(frappe.DoesNotExistError):
			link_channel(workspace=ws_map, raven_channel="RC-does-not-exist")

	def test_link_channel_rejects_unknown_workspace(self):
		from raven_integration.api import link_channel

		with self.assertRaises(frappe.DoesNotExistError):
			link_channel(workspace="RWM-does-not-exist", raven_channel=self.raven_ch)

	def test_link_channel_requires_system_manager(self):
		from raven_integration.api import link_channel, link_workspace

		ws_map = self._track("Raven Workspace Mapping", link_workspace(raven_workspace=self.raven_ws))
		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				link_channel(workspace=ws_map, raven_channel=self.raven_ch)
		finally:
			frappe.set_user("Administrator")

	def test_list_unmapped_channels_requires_system_manager(self):
		from raven_integration.api import link_workspace, list_unmapped_channels

		ws_map = self._track("Raven Workspace Mapping", link_workspace(raven_workspace=self.raven_ws))
		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				list_unmapped_channels(workspace=ws_map)
		finally:
			frappe.set_user("Administrator")

	# --- unified list_workspaces (managed + unmanaged in one list) ---

	def test_list_workspaces_includes_unmanaged_row(self):
		"""An unadopted Raven Workspace surfaces as a mapped=False row carrying the
		Raven id/label/type and neutral rule/flag defaults."""
		from raven_integration.api import list_workspaces

		rows = list_workspaces()
		row = next((w for w in rows if w["raven_workspace"] == self.raven_ws), None)
		self.assertIsNotNone(row, "an unadopted Raven Workspace must appear in list_workspaces")
		self.assertFalse(row["mapped"])
		self.assertIsNone(row["name"])
		# A Raven Workspace autonames from its workspace_name, so name == workspace_name.
		self.assertEqual(row["workspace_label"], self.raven_ws)
		self.assertEqual(row["workspace_type"], "Public")
		self.assertNotIn("rule_combinator", row)
		self.assertNotIn("enabled", row)
		self.assertEqual(row["stale"], 0)

	def test_list_workspaces_managed_row_carries_flag_and_fields(self):
		"""Once adopted, the same workspace is a mapped=True row keeping every field the
		mapping already exposed, and drops out of the unmanaged half."""
		from raven_integration.api import link_workspace, list_workspaces

		name = self._track("Raven Workspace Mapping", link_workspace(raven_workspace=self.raven_ws))
		rows = list_workspaces()
		mapped = [w for w in rows if w["raven_workspace"] == self.raven_ws]
		self.assertEqual(len(mapped), 1, "an adopted workspace must appear exactly once")
		row = mapped[0]
		self.assertTrue(row["mapped"])
		self.assertEqual(row["name"], name)
		for field in ("workspace_label", "workspace_type", "raven_workspace", "stale"):
			self.assertIn(field, row)
		self.assertNotIn("rule_combinator", row)
		self.assertNotIn("enabled", row)

	def test_list_workspaces_counts_the_channels_it_manages(self):
		"""The Channels column reads a real count, not the length of a list the UI
		would otherwise have to fetch per row."""
		from raven_integration.api import list_workspaces

		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = f"RI Count WS {self._suffix}"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		self._track("Raven Workspace Mapping", ws.name)
		for i in range(2):
			ch = frappe.new_doc("Raven Channel Mapping")
			ch.workspace = ws.name
			ch.channel_label = f"ri-count-{i}-{self._suffix}"
			ch.channel_type = "Private"
			ch.flags.skip_raven_create = True
			ch.insert()
			self._track("Raven Channel Mapping", ch.name)

		row = next(w for w in list_workspaces() if w["name"] == ws.name)
		self.assertEqual(row["channel_count"], 2)

	def test_list_workspaces_counts_zero_for_a_workspace_with_no_channels(self):
		"""An adopted workspace that manages nothing yet reports 0, which is true of it."""
		from raven_integration.api import list_workspaces

		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = f"RI Empty WS {self._suffix}"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		self._track("Raven Workspace Mapping", ws.name)

		row = next(w for w in list_workspaces() if w["name"] == ws.name)
		self.assertEqual(row["channel_count"], 0)

	def test_list_workspaces_leaves_the_count_unset_when_unmanaged(self):
		"""An unadopted Raven workspace manages no channels here, which is not the same
		as having none. None, so the UI can leave the cell blank instead of saying 0."""
		from raven_integration.api import list_workspaces

		row = next(w for w in list_workspaces() if w["raven_workspace"] == self.raven_ws)
		self.assertIsNone(row["channel_count"])

	def test_list_workspaces_order_survives_an_edit(self):
		"""Row order must not depend on `modified`. The UI reloads this list after every
		edit, so a row that jumps to the top on being edited drags every row below it
		up one, and the row under the pointer is no longer the row that was there."""
		from raven_integration.api import list_workspaces, set_workspace_type

		names = []
		for i in range(3):
			ws = frappe.new_doc("Raven Workspace Mapping")
			ws.workspace_label = f"RI Order WS {i} {self._suffix}"
			ws.workspace_type = "Private"
			ws.flags.skip_raven_create = True
			ws.insert()
			self._track("Raven Workspace Mapping", ws.name)
			names.append(ws.name)

		def order():
			return [row["name"] for row in list_workspaces() if row["name"] in names]

		before = order()
		# The oldest of the three, so it sits last under any newest-first order and has
		# the furthest to jump.
		set_workspace_type(name=names[0], type="Public")
		self.assertEqual(order(), before, "editing a row must not move it in the list")

	def test_list_channels_order_survives_an_edit(self):
		"""The channel table reloads on every inline edit too, so it needs the same
		stable order as the workspace list."""
		from raven_integration.api import list_channels, set_channel_enabled

		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = f"RI Order CH WS {self._suffix}"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		self._track("Raven Workspace Mapping", ws.name)

		names = []
		for i in range(3):
			ch = frappe.new_doc("Raven Channel Mapping")
			ch.workspace = ws.name
			ch.channel_label = f"ri-order-{i}-{self._suffix}"
			ch.channel_type = "Private"
			ch.flags.skip_raven_create = True
			ch.insert()
			self._track("Raven Channel Mapping", ch.name)
			names.append(ch.name)

		def order():
			return [row["name"] for row in list_channels(workspace=ws.name) if row["name"] in names]

		before = order()
		set_channel_enabled(name=names[0], enabled=False)
		self.assertEqual(order(), before, "editing a row must not move it in the list")

	def test_list_workspaces_managed_rows_come_first(self):
		"""Managed rows precede every unmanaged row."""
		from raven_integration.api import list_workspaces

		# A managed mapping (skip_raven_create → no Raven record needed) alongside the
		# unmanaged self.raven_ws guarantees both halves are present.
		mgd = frappe.new_doc("Raven Workspace Mapping")
		mgd.workspace_label = f"RI Unified Managed WS {self._suffix}"
		mgd.workspace_type = "Private"
		mgd.flags.skip_raven_create = True
		mgd.insert()
		self._track("Raven Workspace Mapping", mgd.name)

		rows = list_workspaces()
		flags = [w["mapped"] for w in rows]
		self.assertIn(True, flags)
		self.assertIn(False, flags)
		# No unmanaged row appears before a managed one.
		first_unmapped = flags.index(False)
		self.assertNotIn(True, flags[first_unmapped:], "managed rows must all precede unmanaged rows")

	# --- unified list_channels (managed + unmanaged in one list) ---

	def test_list_channels_includes_unmanaged_row(self):
		from raven_integration.api import link_workspace, list_channels

		ws_map = self._track("Raven Workspace Mapping", link_workspace(raven_workspace=self.raven_ws))
		rows = list_channels(workspace=ws_map)
		row = next((c for c in rows if c["raven_channel"] == self.raven_ch), None)
		self.assertIsNotNone(row, "an unadopted Raven Channel must appear in list_channels")
		self.assertFalse(row["mapped"])
		self.assertIsNone(row["name"])
		self.assertEqual(
			row["channel_label"],
			frappe.db.get_value("Raven Channel", self.raven_ch, "channel_name"),
		)
		self.assertEqual(row["channel_type"], "Public")
		self.assertNotIn("rule_combinator", row)
		self.assertEqual(row["enabled"], 1)
		self.assertEqual(row["stale"], 0)

	def test_list_channels_managed_rows_come_first(self):
		from raven_integration.api import link_workspace, list_channels

		ws_map = self._track("Raven Workspace Mapping", link_workspace(raven_workspace=self.raven_ws))
		# A managed channel mapping under the same workspace, plus the unmanaged
		# self.raven_ch, exercises both halves.
		mgd = frappe.new_doc("Raven Channel Mapping")
		mgd.channel_label = f"RI Unified Managed Ch {self._suffix}"
		mgd.workspace = ws_map
		mgd.channel_type = "Private"
		mgd.flags.skip_raven_create = True
		mgd.insert()
		self._track("Raven Channel Mapping", mgd.name)

		rows = list_channels(workspace=ws_map)
		flags = [c["mapped"] for c in rows]
		self.assertIn(True, flags)
		self.assertIn(False, flags)
		first_unmapped = flags.index(False)
		self.assertNotIn(True, flags[first_unmapped:], "managed rows must all precede unmanaged rows")

	def test_list_channels_excludes_dm_and_thread_from_unmanaged(self):
		"""The unmanaged half reuses list_unmapped_channels, which drops DM/thread
		channels — assert a DM channel never surfaces as an adoptable row."""
		from raven_integration.api import link_workspace, list_channels

		ws_map = self._track("Raven Workspace Mapping", link_workspace(raven_workspace=self.raven_ws))
		dm = frappe.get_doc(
			{
				"doctype": "Raven Channel",
				"channel_name": f"ri-link-dm-{self._suffix}",
				"workspace": self.raven_ws,
				"type": "Private",
			}
		).insert(ignore_permissions=True)
		self._raven_ch_names.add(dm.name)
		# Flip the flag directly to sidestep Raven's DM-creation validation; the query
		# filter is what we are exercising.
		frappe.db.set_value("Raven Channel", dm.name, "is_direct_message", 1)

		rows = list_channels(workspace=ws_map)
		self.assertNotIn(
			dm.name,
			[c["raven_channel"] for c in rows],
			"a direct-message channel must not appear as an adoptable row",
		)


def _clear_request_cache() -> None:
	"""Drop frappe's per-request memo so a global written mid-test is read again.

	get_active_apps / get_disabled_apps are @request_cache'd, and a test process
	keeps one `frappe.local` for its whole run, so without this the second call
	answers from the snapshot taken before the global changed.
	"""
	cache = getattr(frappe.local, "request_cache", None)
	if cache is not None:
		cache.clear()


class TestIsSetupHonoursDisabledApps(FrappeTestCase):
	"""`bench disable-app raven` leaves raven in installed_apps with its tables
	intact, but frappe stops loading its hooks and running its jobs — so sync
	cannot work. is_setup drives the Settings gate and must not say it can."""

	def setUp(self):
		if "raven" not in frappe.get_active_apps():
			self.skipTest("Raven not installed or already disabled")
		self._previous_disabled = frappe.db.get_global("disabled_apps")
		self.addCleanup(self._restore)

	def _restore(self):
		frappe.db.set_global("disabled_apps", self._previous_disabled or "[]")
		_clear_request_cache()
		# The hook registry is keyed off the active apps and may have been rebuilt
		# without raven's while it was disabled.
		frappe.clear_cache()

	def test_a_disabled_raven_is_reported_as_absent(self):
		from raven_integration.api import is_setup

		self.assertTrue(is_setup()["raven"], "raven is installed and enabled on this bench")

		frappe.db.set_global("disabled_apps", frappe.as_json(["raven"]))
		_clear_request_cache()
		self.assertFalse(
			is_setup()["raven"],
			"a disabled app cannot sync, so the Settings gate must not open on it",
		)


class TestComputeRuleDiffMirrorsSync(FrappeTestCase):
	"""compute_rule_diff drives a confirmation dialog, so its answer has to be the
	one _sync_rule_managed_members will actually reach — including every case where
	the sync moves nobody at all."""

	def setUp(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		self._suffix = frappe.generate_hash(length=6)

		# Someone the FAKE provider does not match, so "removes everyone" and
		# "removes nobody" are different answers.
		self.member = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"rv-mirror-{self._suffix}@example.com",
				"first_name": "MirrorMember",
				"send_welcome_email": 0,
			}
		).insert()

		rw = frappe.get_doc(
			{
				"doctype": "Raven Workspace",
				"workspace_name": f"RI Mirror WS {self._suffix}",
				"type": "Private",
			}
		).insert(ignore_permissions=True)
		self.raven_ws = rw.name
		rc = frappe.get_doc(
			{
				"doctype": "Raven Channel",
				"channel_name": f"ri-mirror-ch-{self._suffix}",
				"workspace": rw.name,
				"type": "Private",
			}
		).insert(ignore_permissions=True)
		self.raven_ch = rc.name

		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = f"RI Mirror WS {self._suffix}"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		frappe.db.set_value("Raven Workspace Mapping", ws.name, "raven_workspace", self.raven_ws)
		self.ws_map = ws.name

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			ch = frappe.new_doc("Raven Channel Mapping")
			ch.channel_label = f"RI Mirror CH {self._suffix}"
			ch.workspace = self.ws_map
			ch.channel_type = "Private"
			ch.flags.skip_raven_create = True
			ch.member_rules_json = frappe.as_json(self._tree())
			ch.insert()
		frappe.db.set_value("Raven Channel Mapping", ch.name, "raven_channel", self.raven_ch)
		self.ch_map = ch.name

		from raven_integration.sync_service import ensure_raven_user

		ensure_raven_user(self.member.name)
		frappe.get_doc(
			{
				"doctype": "Raven Channel Member",
				"channel_id": self.raven_ch,
				"user_id": self.member.name,
				"added_by_rule": 1,
			}
		).insert(ignore_permissions=True)

		self.addCleanup(self._cleanup)

	def _cleanup(self):
		frappe.set_user("Administrator")
		for doctype, name in (
			("Raven Channel Mapping", self.ch_map),
			("Raven Workspace Mapping", self.ws_map),
		):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		# frappe.db.delete bypasses Raven's on_trash guards during teardown.
		frappe.db.delete("Raven Channel Member", {"channel_id": self.raven_ch})
		frappe.db.delete("Raven Channel", {"name": self.raven_ch})
		frappe.db.delete("Raven Workspace Member", {"workspace": self.raven_ws})
		frappe.db.delete("Raven Workspace", {"name": self.raven_ws})
		frappe.db.delete("Raven User", {"user": self.member.name})
		frappe.db.delete("User", {"name": self.member.name})

	def _tree(self):
		return {"conjunctions": [], "conditions": [_leaf("Mirror Rule", rule_type="always-a")]}

	def _diff(self, provider_paths):
		from raven_integration.api import compute_rule_diff

		with patch.object(registry, "_provider_paths", return_value=provider_paths):
			return compute_rule_diff(
				target_doctype="Raven Channel Mapping",
				name=self.ch_map,
				new_rules=self._tree(),
			)

	def test_a_rule_the_provider_answers_still_reports_its_removals(self):
		"""The guards below must not blanket-suppress a real removal."""
		result = self._diff(_FAKE)
		self.assertEqual(result["removed"], 1)
		self.assertEqual(result["removed_users"], [self.member.name])
		self.assertFalse(result["unknown"])

	def test_a_tree_nothing_can_evaluate_is_reported_as_unknown_not_as_a_purge(self):
		"""No provider registered — the provider's app is gone. evaluate_rules
		answers set() for that exactly as it does for "matches nobody", so the
		dialog used to announce every rule-managed member was about to go. The
		real sync runs strict: it raises and removes nobody."""
		result = self._diff([])
		self.assertTrue(result["unknown"], "an unevaluable tree is not a tree that matches nobody")
		self.assertEqual(result["removed"], 0)
		self.assertEqual(result["removed_users"], [])
		self.assertEqual(result["added"], 0)

	def test_a_switched_off_channel_reports_no_change(self):
		"""_sync_rule_managed_members returns skipped/disabled before reading a
		rule, so confirming this change would move nobody."""
		frappe.db.set_value("Raven Channel Mapping", self.ch_map, "enabled", 0)
		result = self._diff(_FAKE)
		self.assertEqual(result["removed"], 0)
		self.assertEqual(result["added"], 0)
		self.assertFalse(result["unknown"])

	def test_a_stale_mapping_reports_no_change(self):
		"""The Raven channel is gone; the sync returns skipped/raven_record_deleted."""
		frappe.db.set_value("Raven Channel Mapping", self.ch_map, "stale", 1)
		result = self._diff(_FAKE)
		self.assertEqual(result["removed"], 0)
		self.assertEqual(result["added"], 0)


_EMPTY_TREE = {"conjunctions": [], "conditions": []}


class TestGetChannelMemberCount(FrappeTestCase):
	"""get_channel's member_count had evaluate_rules' defect: a tree no provider
	could evaluate counted as zero, which reads as "this channel matches nobody"."""

	def setUp(self):
		self._suffix = frappe.generate_hash(length=6)
		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = f"RI Count WS {self._suffix}"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		self.ws_map = ws.name

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			ch = frappe.new_doc("Raven Channel Mapping")
			ch.channel_label = f"RI Count CH {self._suffix}"
			ch.workspace = self.ws_map
			ch.channel_type = "Private"
			ch.flags.skip_raven_create = True
			ch.member_rules_json = frappe.as_json({"conjunctions": [], "conditions": [_leaf("Count Rule")]})
			ch.insert()
		self.ch_map = ch.name

		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", self.ws_map, force=True, ignore_missing=True)
		)
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", self.ch_map, force=True, ignore_missing=True)
		)

	def _detail(self, provider_paths):
		from raven_integration.api import get_channel

		with patch.object(registry, "_provider_paths", return_value=provider_paths):
			return get_channel(self.ch_map)

	def test_a_count_the_providers_answered_is_not_flagged_unknown(self):
		detail = self._detail(_FAKE)
		self.assertEqual(detail["member_count"], 2)
		self.assertFalse(detail["member_count_unknown"])

	def test_a_count_nothing_could_evaluate_is_flagged_unknown(self):
		detail = self._detail([])
		self.assertTrue(
			detail["member_count_unknown"],
			"nobody could evaluate this tree, which is not the same as it matching nobody",
		)

	def test_an_empty_tree_matches_nobody_rather_than_being_unknown(self):
		# Every channel is empty the moment it is created. The tree yielding no
		# opinion answers None exactly as an unevaluable one does, so without the
		# has_active_rules guard a brand new channel opened saying its own
		# membership could not be worked out.
		frappe.db.set_value(
			"Raven Channel Mapping", self.ch_map, "member_rules_json", frappe.as_json(_EMPTY_TREE)
		)
		detail = self._detail(_FAKE)
		self.assertEqual(detail["member_count"], 0)
		self.assertFalse(detail["member_count_unknown"])

	def test_a_tree_whose_every_rule_is_paused_matches_nobody(self):
		# Same None, same reason: no rule offered an opinion. Pausing every rule is a
		# thing the admin did on purpose, not a failure to evaluate.
		paused = _leaf("Count Rule")
		paused["status"] = "Paused"
		frappe.db.set_value(
			"Raven Channel Mapping",
			self.ch_map,
			"member_rules_json",
			frappe.as_json({"conjunctions": [], "conditions": [paused]}),
		)
		detail = self._detail(_FAKE)
		self.assertEqual(detail["member_count"], 0)
		self.assertFalse(detail["member_count_unknown"])


class TestCreateMappingRollsBackALostRace(FrappeTestCase):
	"""_create_mapping retries after a docname collision. before_insert has already
	created the backing Raven record by then, so an attempt that is not rolled back
	leaves it behind, unmanaged, and list_unmapped_workspaces offers it forever."""

	def setUp(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		self._suffix = frappe.generate_hash(length=6)
		self.taken_label = f"RI Race Taken {self._suffix}"
		self.free_label = f"RI Race Free {self._suffix}"
		self.created = None

		# Stands in for the mapping the winning session inserted a moment earlier.
		existing = frappe.new_doc("Raven Workspace Mapping")
		existing.workspace_label = self.taken_label
		existing.workspace_type = "Private"
		existing.flags.skip_raven_create = True
		existing.insert()
		self.existing = existing.name
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		frappe.set_user("Administrator")
		for name in (self.existing, self.created):
			if name and frappe.db.exists("Raven Workspace Mapping", name):
				frappe.delete_doc(
					"Raven Workspace Mapping",
					name,
					force=True,
					ignore_permissions=True,
					ignore_missing=True,
				)
		for label in (self.taken_label, self.free_label):
			frappe.db.delete("Raven Workspace Member", {"workspace": label})
			frappe.db.delete("Raven Workspace", {"name": label})

	def _create_losing_the_first_name(self):
		from raven_integration.api import create_workspace

		with (
			patch(
				"raven_integration.api._next_default_label",
				side_effect=[self.taken_label, self.free_label],
			),
			patch.object(registry, "_provider_paths", return_value=[]),
		):
			self.created = create_workspace()
		return self.created

	def test_a_lost_docname_race_leaves_no_orphan_raven_workspace(self):
		before = frappe.db.count("Raven Workspace")
		self._create_losing_the_first_name()
		self.assertEqual(
			frappe.db.count("Raven Workspace") - before,
			1,
			"only the attempt that inserted a mapping may leave a Raven Workspace behind",
		)
		self.assertFalse(
			frappe.db.exists("Raven Workspace", self.taken_label),
			"the Raven Workspace of the failed attempt must be rolled back",
		)

	def test_a_retried_create_does_not_return_a_duplicate_name_dialog(self):
		"""db_insert msgprints a red "Duplicate Name" before raising, so without
		clear_last_message a create that then succeeded still popped that dialog —
		naming a default the user never chose and never saw."""
		frappe.clear_messages()
		self._create_losing_the_first_name()
		titles = [message.get("title") for message in frappe.get_message_log()]
		self.assertNotIn("Duplicate Name", titles)


class TestPreviewRuleValidatesItsInput(FrappeTestCase):
	"""preview_rule is whitelisted and its three fields reach a dict lookup, a bare
	json.loads and an attribute access. Unvalidated, each is a 500 with a traceback."""

	def _reqcfg_provider(self):
		return {
			"name": "REQCFG2",
			"label": "Requires Config",
			"rule_types": [
				{
					"type": "needs-batch",
					"label": "Needs Batch",
					"fields": [{"fieldname": "batch", "label": "Batch", "reqd": 1}],
				}
			],
			"evaluate": lambda rule_type, config: {config["batch"]},
			"triggers": [],
		}

	def test_rejects_a_provider_that_is_not_text(self):
		"""A list provider reaches dict.get() and raises TypeError: unhashable."""
		from raven_integration.api import preview_rule

		with self.assertRaises(frappe.ValidationError) as cm:
			preview_rule({"provider": ["x"], "rule_type": "always-ab", "config": {}})
		self.assertIn("provider", str(cm.exception))

	def test_rejects_a_rule_type_that_is_not_text(self):
		"""Reported as a type error naming the field, not as "provider X has no
		rule type 7" — the request never chose a rule type at all."""
		from raven_integration.api import preview_rule

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.ValidationError) as cm:
				preview_rule({"provider": "FAKE", "rule_type": 7, "config": {}})
		self.assertIn("must be text", str(cm.exception))

	def test_rejects_a_config_that_is_not_an_object(self):
		"""registry.validate_rule_config json.loads a string config with no guard,
		so malformed text used to surface as a JSONDecodeError."""
		from raven_integration.api import preview_rule

		with patch.object(registry, "_provider_paths", return_value=_FAKE):
			with self.assertRaises(frappe.ValidationError):
				preview_rule({"provider": "FAKE", "rule_type": "always-ab", "config": "{"})

	def test_rejects_a_config_that_is_a_list(self):
		"""A list config reaches cfg.get() in the reqd-field loop -> AttributeError."""
		from raven_integration.api import preview_rule

		with patch.object(registry, "_load_providers", return_value={"REQCFG2": self._reqcfg_provider()}):
			with self.assertRaises(frappe.ValidationError):
				preview_rule({"provider": "REQCFG2", "rule_type": "needs-batch", "config": [1]})


class TestMutatingEndpointsAreNotReachableByGet(FrappeTestCase):
	"""Hardening, not a live hole: frappe already rolls back a non-unsafe method
	and every enqueue here is enqueue_after_commit. But a bare @frappe.whitelist()
	allows GET, and CSRF is skipped for non-unsafe methods — so the declaration is
	the only thing saying these change state. frappe.client does the same."""

	_STATE_CHANGING = (
		"enable_integration",
		"create_workspace",
		"update_workspace",
		"delete_workspace",
		"recreate_workspace",
		"set_workspace_type",
		"set_workspace_label",
		"create_channel",
		"update_channel",
		"delete_channel",
		"recreate_channel",
		"set_channel_enabled",
		"set_channel_type",
		"set_channel_label",
		"set_channel_rule_status",
		"reconcile_now",
		"link_workspace",
		"link_channel",
	)

	_READ_ONLY = (
		"is_setup",
		"list_providers",
		"list_workspaces",
		"get_workspace",
		"list_workspace_members",
		"list_channels",
		"get_channel",
		"preview_rule",
		"compute_rule_diff",
		"list_unmapped_workspaces",
		"list_unmapped_channels",
	)

	def _methods(self, name: str):
		from raven_integration import api

		return frappe.allowed_http_methods_for_whitelisted_func[getattr(api, name)]

	def test_every_state_changing_endpoint_refuses_get(self):
		for name in self._STATE_CHANGING:
			with self.subTest(endpoint=name):
				self.assertNotIn("GET", self._methods(name))
				self.assertIn("POST", self._methods(name))

	def test_the_delete_endpoints_also_accept_delete(self):
		for name in ("delete_workspace", "delete_channel"):
			with self.subTest(endpoint=name):
				self.assertIn("DELETE", self._methods(name))

	def test_read_only_endpoints_stay_reachable_by_get(self):
		for name in self._READ_ONLY:
			with self.subTest(endpoint=name):
				self.assertIn("GET", self._methods(name))


class TestCaseOnlyNameCollision(FrappeTestCase):
	"""The docname column collates utf8mb4_unicode_ci, so two mappings whose labels
	differ only in case cannot both exist. db.exists is case-insensitive for the
	same reason, and comparing its answer to the *new* name let a different doc's
	row through — the rename then died on the primary key."""

	def setUp(self):
		self._suffix = frappe.generate_hash(length=6)
		ws = frappe.new_doc("Raven Workspace Mapping")
		ws.workspace_label = f"RI Case WS {self._suffix}"
		ws.workspace_type = "Private"
		ws.flags.skip_raven_create = True
		ws.insert()
		self.ws_map = ws.name
		self.general = self._channel(f"General {self._suffix}")
		self.sales = self._channel(f"Sales {self._suffix}")
		self.addCleanup(self._cleanup)

	def _channel(self, label: str) -> str:
		ch = frappe.new_doc("Raven Channel Mapping")
		ch.channel_label = label
		ch.workspace = self.ws_map
		ch.channel_type = "Private"
		ch.flags.skip_raven_create = True
		ch.insert()
		return ch.name

	def _cleanup(self):
		frappe.set_user("Administrator")
		for name in frappe.get_all("Raven Channel Mapping", filters={"workspace": self.ws_map}, pluck="name"):
			frappe.delete_doc(
				"Raven Channel Mapping", name, force=True, ignore_permissions=True, ignore_missing=True
			)
		frappe.delete_doc(
			"Raven Workspace Mapping",
			self.ws_map,
			force=True,
			ignore_permissions=True,
			ignore_missing=True,
		)

	def test_renaming_onto_another_mappings_name_in_another_case_is_refused(self):
		from raven_integration.api import set_channel_label

		with self.assertRaises(frappe.ValidationError) as cm:
			set_channel_label(name=self.sales, label=f"general {self._suffix}")
		self.assertIn("already called", str(cm.exception))
		self.assertTrue(frappe.db.exists("Raven Channel Mapping", self.sales))

	def test_re_casing_a_mappings_own_name_still_renames_it(self):
		"""The reason the check is loose in the first place: rename_doc allows a
		doc to be renamed to a different casing of its own name."""
		from raven_integration.api import set_channel_label

		result = set_channel_label(name=self.sales, label=f"sales {self._suffix}")
		self.assertEqual(result["name"], f"RCM-sales {self._suffix}")
		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", result["name"], "channel_label"),
			f"sales {self._suffix}",
		)


class TestRavenWorkspaceCaseOnlyCollision(FrappeTestCase):
	"""_rename_raven_workspace carried the same loose check against Raven Workspace,
	whose docname *is* its display name."""

	def setUp(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		self._suffix = frappe.generate_hash(length=6)
		self._raven_ws_names = set()

		self.other_ws = self._raven_workspace(f"RI WsCase Other {self._suffix}")
		self.mine_ws = self._raven_workspace(f"RI WsCase Mine {self._suffix}")

		ws_map = frappe.new_doc("Raven Workspace Mapping")
		ws_map.workspace_label = self.mine_ws
		ws_map.workspace_type = "Private"
		ws_map.flags.skip_raven_create = True
		ws_map.insert()
		frappe.db.set_value("Raven Workspace Mapping", ws_map.name, "raven_workspace", self.mine_ws)
		self.ws_map = ws_map.name
		self.addCleanup(self._cleanup)

	def _raven_workspace(self, name: str) -> str:
		doc = frappe.get_doc(
			{"doctype": "Raven Workspace", "workspace_name": name, "type": "Private"}
		).insert(ignore_permissions=True)
		self._raven_ws_names.add(doc.name)
		return doc.name

	def _cleanup(self):
		frappe.set_user("Administrator")
		if frappe.db.exists("Raven Workspace Mapping", self.ws_map):
			frappe.delete_doc(
				"Raven Workspace Mapping",
				self.ws_map,
				force=True,
				ignore_permissions=True,
				ignore_missing=True,
			)
		for name in self._raven_ws_names:
			frappe.db.delete("Raven Workspace Member", {"workspace": name})
			frappe.db.delete("Raven Workspace", {"name": name})

	def test_renaming_onto_another_raven_workspace_in_another_case_is_refused(self):
		from raven_integration.api import set_workspace_label

		clashing = self.other_ws.lower()
		self._raven_ws_names.add(clashing)
		with self.assertRaises(frappe.ValidationError) as cm:
			set_workspace_label(name=self.ws_map, label=clashing)
		self.assertIn("already exists", str(cm.exception))
		self.assertTrue(frappe.db.exists("Raven Workspace", self.mine_ws))


class TestLinkChannelValidatesTheChannel(FrappeTestCase):
	"""link_channel adopted any Raven Channel id posted at it — list_unmapped_channels
	filters DMs, threads and other workspaces, but the endpoint did not."""

	def setUp(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		self._suffix = frappe.generate_hash(length=6)
		self._raven_ws_names = set()
		self._raven_ch_names = set()
		self._mappings: list[tuple[str, str]] = []

		self.raven_ws = self._raven_workspace(f"RI Adopt WS {self._suffix}")
		self.other_ws = self._raven_workspace(f"RI Adopt Other {self._suffix}")
		self.own_ch = self._raven_channel(f"ri-adopt-own-{self._suffix}", self.raven_ws)
		self.foreign_ch = self._raven_channel(f"ri-adopt-foreign-{self._suffix}", self.other_ws)
		self.dm_ch = self._raven_channel(f"ri-adopt-dm-{self._suffix}", self.raven_ws)
		# Flipped directly, the way test_list_channels_excludes_dm_and_thread does:
		# Raven's own DM creation path wants a canonical two-user channel_name.
		frappe.db.set_value("Raven Channel", self.dm_ch, "is_direct_message", 1)

		from raven_integration.api import link_workspace

		self.ws_map = self._track("Raven Workspace Mapping", link_workspace(raven_workspace=self.raven_ws))
		self.addCleanup(self._cleanup)

	def _raven_workspace(self, name: str) -> str:
		doc = frappe.get_doc(
			{"doctype": "Raven Workspace", "workspace_name": name, "type": "Private"}
		).insert(ignore_permissions=True)
		self._raven_ws_names.add(doc.name)
		return doc.name

	def _raven_channel(self, name: str, workspace: str) -> str:
		doc = frappe.get_doc(
			{"doctype": "Raven Channel", "channel_name": name, "workspace": workspace, "type": "Private"}
		).insert(ignore_permissions=True)
		self._raven_ch_names.add(doc.name)
		return doc.name

	def _track(self, doctype: str, name: str) -> str:
		self._mappings.append((doctype, name))
		return name

	def _cleanup(self):
		frappe.set_user("Administrator")
		for doctype, name in reversed(self._mappings):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		for name in self._raven_ch_names:
			frappe.db.delete("Raven Channel Member", {"channel_id": name})
			frappe.db.delete("Raven Channel", {"name": name})
		for name in self._raven_ws_names:
			frappe.db.delete("Raven Workspace Member", {"workspace": name})
			frappe.db.delete("Raven Workspace", {"name": name})

	def test_adopts_a_regular_channel_of_the_workspace(self):
		from raven_integration.api import link_channel

		name = self._track(
			"Raven Channel Mapping", link_channel(workspace=self.ws_map, raven_channel=self.own_ch)
		)
		self.assertEqual(frappe.db.get_value("Raven Channel Mapping", name, "raven_channel"), self.own_ch)

	def test_refuses_a_channel_from_another_raven_workspace(self):
		"""Its members would be joined to this mapping's Raven workspace while
		on_trash evicts them against the one the channel actually lives in."""
		from raven_integration.api import link_channel

		with self.assertRaises(frappe.ValidationError) as cm:
			link_channel(workspace=self.ws_map, raven_channel=self.foreign_ch)
		self.assertIn("lives in Raven workspace", str(cm.exception))
		self.assertFalse(frappe.db.exists("Raven Channel Mapping", {"raven_channel": self.foreign_ch}))

	def test_refuses_a_direct_message(self):
		"""Rules would insert matched users into a two-person conversation."""
		from raven_integration.api import link_channel

		with self.assertRaises(frappe.ValidationError):
			link_channel(workspace=self.ws_map, raven_channel=self.dm_ch)
		self.assertFalse(frappe.db.exists("Raven Channel Mapping", {"raven_channel": self.dm_ch}))

	def test_refuses_a_thread(self):
		from raven_integration.api import link_channel

		frappe.db.set_value("Raven Channel", self.own_ch, "is_thread", 1)
		with self.assertRaises(frappe.ValidationError):
			link_channel(workspace=self.ws_map, raven_channel=self.own_ch)
		self.assertFalse(frappe.db.exists("Raven Channel Mapping", {"raven_channel": self.own_ch}))

	def test_refuses_when_the_parent_mapping_has_no_raven_workspace(self):
		"""Nothing can be adopted into a mapping with no Raven workspace behind it;
		list_unmapped_channels returns an empty list for exactly this case."""
		from raven_integration.api import link_channel

		frappe.db.set_value("Raven Workspace Mapping", self.ws_map, "raven_workspace", None)
		with self.assertRaises(frappe.ValidationError):
			link_channel(workspace=self.ws_map, raven_channel=self.own_ch)
