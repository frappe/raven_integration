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
		self.assertEqual(
			frappe.db.get_single_value("Raven Membership Settings", "enabled"), 1
		)
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
			create_workspace(label=42, type="Private", rules=[])

	def test_create_workspace_rejects_invalid_type(self):
		from raven_integration.api import create_workspace

		with self.assertRaises(frappe.ValidationError):
			create_workspace(label="Test", type="weird", rules=[])

	def test_create_workspace_persists_and_returns_name(self):
		from raven_integration.api import create_workspace

		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		with patch.object(registry, "_provider_paths", return_value=[]):
			name = create_workspace(
				label="API Test WS",
				type="Private",
				rules=[],
			)
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
				lambda n=n: frappe.delete_doc(
					"Raven Workspace Mapping", n, force=True, ignore_missing=True
				)
			)
		label_a = frappe.db.get_value("Raven Workspace Mapping", a, "workspace_label")
		label_b = frappe.db.get_value("Raven Workspace Mapping", b, "workspace_label")
		self.assertRegex(label_a, r"^Workspace \d+$")
		self.assertRegex(label_b, r"^Workspace \d+$")
		self.assertNotEqual(label_a, label_b, "consecutive auto-creates must differ")

	def test_create_workspace_with_fake_rules_persists(self):
		from raven_integration.api import create_workspace

		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		with patch.object(
			registry,
			"_provider_paths",
			return_value=["raven_integration.tests.fake_provider.get_provider"],
		):
			name = create_workspace(
				label="API Test WS Fake",
				type="Private",
				rules=[
					{
						"label": "Rule 1",
						"provider": "FAKE",
						"rule_type": "always-ab",
						"status": "Active",
						"config": {},
					}
				],
			)
		self.assertTrue(name.startswith("RWM-"))
		self.addCleanup(lambda: frappe.delete_doc("Raven Workspace Mapping", name, force=True))

	def test_create_workspace_persists_combinator(self):
		from raven_integration.api import create_workspace, get_workspace

		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		with patch.object(registry, "_provider_paths", return_value=[]):
			name = create_workspace(
				label="Combo WS",
				type="Private",
				rules=[],
				combinator="All (AND)",
			)
		self.addCleanup(lambda: frappe.delete_doc("Raven Workspace Mapping", name, force=True))
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", name, "rule_combinator"),
			"All (AND)",
		)
		self.assertEqual(get_workspace(name)["rule_combinator"], "All (AND)")

	def test_invalid_combinator_rejected(self):
		from raven_integration.api import create_workspace

		with self.assertRaises(frappe.ValidationError):
			create_workspace(
				label="Bad", type="Private", rules=[], combinator="XOR"
			)

	def test_update_workspace_rejects_unknown_name(self):
		from raven_integration.api import update_workspace

		with self.assertRaises(frappe.DoesNotExistError):
			update_workspace(
				name="RWM-does-not-exist",
				label="New Label",
				type="Private",
				rules=[],
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
			lambda: frappe.delete_doc(
				"Raven Workspace Mapping", ws.name, force=True, ignore_missing=True
			)
		)

		ch = frappe.new_doc("Raven Channel Mapping")
		ch.channel_label = "API Cascade Delete Channel"
		ch.workspace = ws.name
		ch.channel_type = "Private"
		ch.flags.skip_raven_create = True
		ch.insert()
		self.addCleanup(
			lambda: frappe.delete_doc(
				"Raven Channel Mapping", ch.name, force=True, ignore_missing=True
			)
		)

		# Runs as Administrator (a System Manager), satisfying _require_system_manager.
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
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", self.workspace, force=True)
		)

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
			create_channel(
				workspace=self.workspace, label=42, type="Private", rules=[]
			)

	def test_create_channel_rejects_invalid_type(self):
		from raven_integration.api import create_channel

		with self.assertRaises(frappe.ValidationError):
			create_channel(
				workspace=self.workspace,
				label="Test Ch",
				type="weird",
				rules=[],
			)

	def test_create_channel_rejects_unknown_workspace(self):
		from raven_integration.api import create_channel

		with self.assertRaises(frappe.ValidationError):
			create_channel(
				workspace="RWM-does-not-exist",
				label="Test Ch",
				type="Private",
				rules=[],
			)

	def test_create_channel_persists_and_returns_name(self):
		from raven_integration.api import create_channel, create_workspace

		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		# Create a workspace with a real Raven link so the channel can be created under it.
		with patch.object(registry, "_provider_paths", return_value=[]):
			ws_name = create_workspace(
				label="Ch API Real WS", type="Private", rules=[]
			)
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", ws_name, force=True)
		)
		with patch.object(registry, "_provider_paths", return_value=[]):
			name = create_channel(
				workspace=ws_name,
				label="API Test Channel",
				type="Private",
				rules=[],
			)
		self.assertTrue(name.startswith("RCM-"))
		self.addCleanup(lambda: frappe.delete_doc("Raven Channel Mapping", name, force=True))

	def test_create_channel_auto_names_when_label_omitted(self):
		from raven_integration.api import create_channel, create_workspace

		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		with patch.object(registry, "_provider_paths", return_value=[]):
			ws_name = create_workspace(label="Ch AutoName WS", type="Private", rules=[])
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", ws_name, force=True)
		)
		with patch.object(registry, "_provider_paths", return_value=[]):
			name = create_channel(workspace=ws_name)
		self.addCleanup(
			lambda: frappe.delete_doc(
				"Raven Channel Mapping", name, force=True, ignore_missing=True
			)
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
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", ws.name, force=True)
		)

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
			lambda: frappe.delete_doc(
				"Raven Workspace Mapping", ws.name, force=True, ignore_missing=True
			)
		)

		ch = frappe.new_doc("Raven Channel Mapping")
		ch.channel_label = "Reconcile Now Test Channel"
		ch.workspace = ws.name
		ch.channel_type = "Private"
		ch.flags.skip_raven_create = True
		ch.insert()
		self.addCleanup(
			lambda: frappe.delete_doc(
				"Raven Channel Mapping", ch.name, force=True, ignore_missing=True
			)
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
			reconcile_now(
				target_doctype="Raven Workspace Mapping", name="RWM-does-not-exist"
			)

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
			result = preview_rule(
				{"provider": "FAKE", "rule_type": "always-ab", "config": {}}
			)
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
			ws.rule_combinator = "Any (OR)"
			ws.flags.skip_raven_create = True
			ws.append(
				"member_rules",
				{
					"label": "Rule 1",
					"provider": "FAKE",
					"rule_type": "always-ab",
					"status": "Active",
					"config": "{}",
				},
			)
			ws.insert()
		self.workspace_name = ws.name
		self.addCleanup(
			lambda: frappe.delete_doc(
				"Raven Workspace Mapping", self.workspace_name, force=True
			)
		)

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
				target_doctype="Raven Workspace Mapping",
				name=self.workspace_name,
				new_rules=[],
			)
		self.assertEqual(result, {"added": 0, "removed": 0, "removed_users": []})

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
				target_doctype="Raven Workspace Mapping",
				name=self.workspace_name,
				new_rules=[
					{"provider": "FAKE", "rule_type": "always-b", "config": {}, "status": "Active"}
				],
			)
		self.assertEqual(result["removed"], 0)
		self.assertEqual(result["removed_users"], [])

	def test_defaults_to_saved_rules_so_a_combinator_switch_can_be_previewed(self):
		"""Omitting new_rules previews a combinator change on its own."""
		from raven_integration.api import compute_rule_diff

		with patch.object(
			registry,
			"_provider_paths",
			return_value=["raven_integration.tests.fake_provider.get_provider"],
		):
			result = compute_rule_diff(
				target_doctype="Raven Workspace Mapping",
				name=self.workspace_name,
				combinator="All (AND)",
			)
		for key in ("added", "removed", "removed_users"):
			self.assertIn(key, result)

	def test_rejects_an_invalid_combinator(self):
		from raven_integration.api import compute_rule_diff

		with self.assertRaises(frappe.ValidationError):
			compute_rule_diff(
				target_doctype="Raven Workspace Mapping",
				name=self.workspace_name,
				combinator="Sometimes (MAYBE)",
			)

	def test_removed_users_capped_at_ten(self):
		from raven_integration.api import compute_rule_diff

		with patch.object(
			registry,
			"_provider_paths",
			return_value=["raven_integration.tests.fake_provider.get_provider"],
		):
			result = compute_rule_diff(
				target_doctype="Raven Workspace Mapping",
				name=self.workspace_name,
				new_rules=[],
			)
		self.assertLessEqual(len(result["removed_users"]), 10)

	def test_rejects_invalid_target_doctype(self):
		from raven_integration.api import compute_rule_diff

		with self.assertRaises(frappe.ValidationError):
			compute_rule_diff(
				target_doctype="LMS Batch",
				name=self.workspace_name,
				new_rules=[],
			)

	def test_requires_system_manager(self):
		from raven_integration.api import compute_rule_diff

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				compute_rule_diff(
					target_doctype="Raven Workspace Mapping",
					name=self.workspace_name,
					new_rules=[],
				)
		finally:
			frappe.set_user("Administrator")


class TestMappingEnabledToggle(FrappeTestCase):
	"""set_workspace_enabled / set_channel_enabled — per-mapping disabled flag."""

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
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", self.workspace, force=True)
		)

		ch = frappe.new_doc("Raven Channel Mapping")
		ch.channel_label = "Toggle Test Channel"
		ch.workspace = self.workspace
		ch.channel_type = "Private"
		ch.flags.skip_raven_create = True
		ch.insert()
		self.channel = ch.name
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", self.channel, force=True)
		)

	def test_set_workspace_enabled_toggles_enabled_flag(self):
		from raven_integration.api import set_workspace_enabled

		result = set_workspace_enabled(name=self.workspace, enabled=False)
		self.assertFalse(result["enabled"])
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.workspace, "enabled"), 0
		)

		result = set_workspace_enabled(name=self.workspace, enabled=True)
		self.assertTrue(result["enabled"])
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.workspace, "enabled"), 1
		)

	def test_set_channel_enabled_toggles_enabled_flag(self):
		from raven_integration.api import set_channel_enabled

		result = set_channel_enabled(name=self.channel, enabled=False)
		self.assertFalse(result["enabled"])
		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.channel, "enabled"), 0
		)

		result = set_channel_enabled(name=self.channel, enabled=True)
		self.assertTrue(result["enabled"])
		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.channel, "enabled"), 1
		)

	def test_set_workspace_enabled_rejects_non_bool(self):
		from raven_integration.api import set_workspace_enabled

		with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
			set_workspace_enabled(name=self.workspace, enabled="nope")

	def test_set_channel_enabled_rejects_non_string_name(self):
		from raven_integration.api import set_channel_enabled

		with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
			set_channel_enabled(name=42, enabled=True)

	def test_set_workspace_enabled_requires_system_manager(self):
		from raven_integration.api import set_workspace_enabled

		frappe.set_user(self.non_admin.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				set_workspace_enabled(name=self.workspace, enabled=True)
		finally:
			frappe.set_user("Administrator")

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
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", self.workspace, force=True)
		)

		ch = frappe.new_doc("Raven Channel Mapping")
		ch.channel_label = "Type Test Channel"
		ch.workspace = self.workspace
		ch.channel_type = "Private"
		ch.flags.skip_raven_create = True
		ch.insert()
		self.channel = ch.name
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", self.channel, force=True)
		)

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

	def test_set_workspace_combinator_changes_field(self):
		from raven_integration.api import set_workspace_combinator

		result = set_workspace_combinator(name=self.workspace, combinator="All (AND)")
		self.assertEqual(result["combinator"], "All (AND)")
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", self.workspace, "rule_combinator"),
			"All (AND)",
		)

	def test_set_channel_combinator_changes_field(self):
		from raven_integration.api import set_channel_combinator

		result = set_channel_combinator(name=self.channel, combinator="All (AND)")
		self.assertEqual(result["combinator"], "All (AND)")
		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.channel, "rule_combinator"),
			"All (AND)",
		)

	def test_set_workspace_combinator_rejects_invalid(self):
		from raven_integration.api import set_workspace_combinator

		with self.assertRaises(frappe.ValidationError):
			set_workspace_combinator(name=self.workspace, combinator="MAYBE")

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
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Workspace Mapping", self.workspace, force=True)
		)

		ch = frappe.new_doc("Raven Channel Mapping")
		ch.channel_label = "Label Test Channel"
		ch.workspace = self.workspace
		ch.channel_type = "Private"
		ch.flags.skip_raven_create = True
		ch.insert()
		self.channel = ch.name
		self.addCleanup(
			lambda: frappe.delete_doc("Raven Channel Mapping", self.channel, force=True)
		)

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
			lambda: frappe.delete_doc(
				"Raven Workspace Mapping", other.name, force=True, ignore_missing=True
			)
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
			lambda: frappe.delete_doc(
				"Raven Workspace Mapping", ws.name, force=True, ignore_missing=True
			)
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
				frappe.delete_doc(
					"Raven Workspace Mapping", n, force=True, ignore_missing=True
				)
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
			lambda: frappe.delete_doc(
				"Raven Workspace Mapping", ws.name, force=True, ignore_missing=True
			)
		)

		first = frappe.new_doc("Raven Channel Mapping")
		first.channel_label = "Channel 9001"
		first.workspace = ws.name
		first.channel_type = "Private"
		first.flags.skip_raven_create = True
		first.insert()
		self.assertEqual(first.name, "RCM-Channel 9001")

		renamed = _set_mapping_label(
			"Raven Channel Mapping", first.name, "Renamed Channel Deadlock"
		)
		self.addCleanup(
			lambda: frappe.delete_doc(
				"Raven Channel Mapping", renamed, force=True, ignore_missing=True
			)
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
			lambda: frappe.delete_doc(
				"Raven Channel Mapping", second.name, force=True, ignore_missing=True
			)
		)
		self.assertEqual(second.name, "RCM-Channel 9001")


class TestMissingMappingFailsLoudly(FrappeTestCase):
	"""db.set_value on a missing row is a silent no-op — every set_* endpoint must
	reject an unknown name instead of reporting a change that never landed."""

	def test_set_workspace_enabled_rejects_unknown_name(self):
		from raven_integration.api import set_workspace_enabled

		with self.assertRaises(frappe.DoesNotExistError):
			set_workspace_enabled(name="RWM-does-not-exist", enabled=True)

	def test_set_workspace_type_rejects_unknown_name(self):
		from raven_integration.api import set_workspace_type

		with self.assertRaises(frappe.DoesNotExistError):
			set_workspace_type(name="RWM-does-not-exist", type="Public")

	def test_set_workspace_combinator_rejects_unknown_name(self):
		from raven_integration.api import set_workspace_combinator

		with self.assertRaises(frappe.DoesNotExistError):
			set_workspace_combinator(name="RWM-does-not-exist", combinator="All (AND)")

	def test_set_channel_enabled_rejects_unknown_name(self):
		from raven_integration.api import set_channel_enabled

		with self.assertRaises(frappe.DoesNotExistError):
			set_channel_enabled(name="RCM-does-not-exist", enabled=True)

	def test_set_channel_type_rejects_unknown_name(self):
		from raven_integration.api import set_channel_type

		with self.assertRaises(frappe.DoesNotExistError):
			set_channel_type(name="RCM-does-not-exist", type="Public")

	def test_set_channel_combinator_rejects_unknown_name(self):
		from raven_integration.api import set_channel_combinator

		with self.assertRaises(frappe.DoesNotExistError):
			set_channel_combinator(name="RCM-does-not-exist", combinator="All (AND)")


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
				"Raven Workspace Mapping", self.ws_map, force=True, ignore_permissions=True, ignore_missing=True
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
			{"doctype": "Raven Workspace", "workspace_name": f"RI Lbl Occupied {self._suffix}", "type": "Private"}
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
				"Raven Workspace Mapping", self.ws_map, force=True, ignore_permissions=True, ignore_missing=True
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
		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", self.ch_map, "channel_type"), "Public"
		)
		self.assertEqual(frappe.db.get_value("Raven Channel", self.raven_ch, "type"), "Public")

	def test_update_workspace_propagates_type_to_raven(self):
		"""The full update path (not just the inline setter) must propagate too."""
		from raven_integration.api import update_workspace

		update_workspace(name=self.ws_map, label=self.raven_ws, type="Public", rules=[])

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
		self.assertEqual(frappe.db.count("Raven Workspace"), before, "linking must not create a Raven Workspace")
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", name, "raven_workspace"), self.raven_ws
		)
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", name, "workspace_label"), self.raven_ws
		)
		self.assertEqual(
			frappe.db.get_value("Raven Workspace Mapping", name, "workspace_type"), "Public"
		)

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
		self.assertEqual(
			frappe.db.get_value("Raven Channel Mapping", name, "raven_channel"), self.raven_ch
		)
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
		self.assertIsNone(row["rule_combinator"])
		self.assertEqual(row["enabled"], 1)
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
		for field in (
			"workspace_label",
			"workspace_type",
			"rule_combinator",
			"raven_workspace",
			"enabled",
			"stale",
		):
			self.assertIn(field, row)

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
		self.assertIsNone(row["rule_combinator"])
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
