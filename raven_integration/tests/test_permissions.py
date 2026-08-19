from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration.permissions import (
	MANAGED_DOCTYPES,
	manager_roles,
	remove_manager_docperms,
	require_manager,
	revoke_undeclared_manager_docperms,
	sync_manager_docperms,
)

_HOOK = "raven_integration_manager_roles"
_ROLE = "Raven Test Manager"


@contextmanager
def declared_roles(*roles: str):
	"""Pretend a host app declares these manager roles.

	The hook is patched rather than read from the bench so the suite says the same
	thing on a site without LMS installed. Every other hook still resolves for real.
	"""
	real = frappe.get_hooks

	def fake(hook=None, *args, **kwargs):
		if hook == _HOOK:
			return list(roles)
		return real(hook, *args, **kwargs)

	with patch.object(frappe, "get_hooks", fake):
		yield


class ManagerRoleTestCase(FrappeTestCase):
	"""Fixtures for a throwaway manager role and users holding it.

	tearDown restores Administrator before addCleanup runs, so the fixture deletes
	are not attempted as whichever non-admin user the test switched to.
	"""

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def ensure_role(self) -> None:
		# desk_access = 0 deliberately, to match LMS's Moderator. It makes the holder a
		# Website User, which Raven does not auto-provision as a Raven User — the
		# condition the workspace/channel create paths have to handle.
		frappe.get_doc({"doctype": "Role", "role_name": _ROLE, "desk_access": 0}).insert(
			ignore_if_duplicate=True
		)
		self.addCleanup(self.drop_docperms)

	def make_user(self, email: str, first_name: str, with_role: bool = False):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first_name,
				"send_welcome_email": 0,
				"roles": [{"role": _ROLE}] if with_role else [],
			}
		).insert()
		# force=True: inserting a User also creates a Contact that links back to it.
		self.addCleanup(frappe.delete_doc, "User", user.name, force=True, ignore_permissions=True)
		return user

	def drop_docperms(self) -> None:
		for doctype in MANAGED_DOCTYPES:
			frappe.db.delete("Custom DocPerm", {"parent": doctype, "role": _ROLE})

	def docperm_rows(self) -> list[dict]:
		return frappe.get_all(
			"Custom DocPerm",
			filters={"role": _ROLE, "parent": ("in", list(MANAGED_DOCTYPES))},
			fields=["parent", "read", "write", "create", "delete"],
		)


class TestManagerRoles(FrappeTestCase):
	def test_system_manager_is_always_a_manager(self):
		with declared_roles():
			self.assertEqual(manager_roles(), ["System Manager"])

	def test_declared_roles_are_appended(self):
		with declared_roles("Moderator"):
			self.assertEqual(manager_roles(), ["System Manager", "Moderator"])

	def test_duplicates_are_collapsed(self):
		"""Two apps naming the same role, or one naming System Manager, must not
		produce a duplicate — only_for is a membership test, but the list is also
		what the error message shows the user."""
		with declared_roles("Moderator", "System Manager", "Moderator"):
			self.assertEqual(manager_roles(), ["System Manager", "Moderator"])

	def test_empty_entries_are_ignored(self):
		with declared_roles("", "Moderator"):
			self.assertEqual(manager_roles(), ["System Manager", "Moderator"])


class TestRequireManager(ManagerRoleTestCase):
	def setUp(self):
		self.ensure_role()
		self.manager = self.make_user("rv-perm-manager@example.com", "PermManager", with_role=True)
		self.plain = self.make_user("rv-perm-plain@example.com", "PermPlain")

	def test_declared_role_passes(self):
		with declared_roles(_ROLE):
			frappe.set_user(self.manager.name)
			require_manager()  # must not raise

	def test_roleless_user_is_rejected(self):
		with declared_roles(_ROLE):
			frappe.set_user(self.plain.name)
			with self.assertRaises(frappe.PermissionError):
				require_manager()

	def test_role_is_not_a_manager_unless_declared(self):
		"""Holding the role is not enough — a host app has to declare it."""
		with declared_roles():
			frappe.set_user(self.manager.name)
			with self.assertRaises(frappe.PermissionError):
				require_manager()


class TestSyncManagerDocperms(ManagerRoleTestCase):
	def setUp(self):
		self.ensure_role()

	def test_grants_every_managed_doctype(self):
		with declared_roles(_ROLE):
			sync_manager_docperms()
		rows = {r["parent"]: r for r in self.docperm_rows()}
		self.assertEqual(set(rows), set(MANAGED_DOCTYPES))
		# The mapping doctypes need the full set; the Single only read/write.
		ws = rows["Raven Workspace Mapping"]
		self.assertTrue(ws["read"] and ws["write"] and ws["create"] and ws["delete"])
		settings = rows["Raven Membership Settings"]
		self.assertTrue(settings["read"] and settings["write"])

	def test_is_idempotent(self):
		with declared_roles(_ROLE):
			sync_manager_docperms()
			before = len(self.docperm_rows())
			sync_manager_docperms()
		self.assertEqual(len(self.docperm_rows()), before)

	def test_unknown_role_is_skipped(self):
		"""A host app can declare a role it creates later; that must not crash migrate."""
		missing = "Raven Role That Does Not Exist"
		with declared_roles(missing):
			sync_manager_docperms()  # must not raise
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"role": missing}))

	def test_remove_drops_the_grants(self):
		with declared_roles(_ROLE):
			sync_manager_docperms()
			self.assertTrue(self.docperm_rows())
			remove_manager_docperms()
		self.assertFalse(self.docperm_rows())

	def test_remove_drops_a_grant_whose_declaring_app_is_already_gone(self):
		# before_uninstall can run after the host app has been removed: get_hooks
		# resolves active apps, so the role that needs cleaning up is exactly the one
		# nothing names any more, and the row would be left pointing at a doctype that
		# is about to be dropped.
		with declared_roles(_ROLE):
			sync_manager_docperms()
			self.assertTrue(self.docperm_rows())
		with declared_roles():
			remove_manager_docperms()
		self.assertFalse(self.docperm_rows())

	def test_an_owner_only_rule_does_not_cancel_the_grant(self):
		"""An "Apply Only to Owner" rule for the same role must not answer the probe.

		If it does, migrate skips the grant, require_manager() still lets the role in,
		and the endpoint's own insert() then raises PermissionError half way through —
		the exact failure this module exists to prevent.
		"""
		from frappe.permissions import add_permission

		doctype = "Raven Channel Mapping"
		owner_rule = add_permission(doctype, _ROLE, 0)
		self.assertTrue(owner_rule)
		frappe.db.set_value("Custom DocPerm", owner_rule, "if_owner", 1)

		with declared_roles(_ROLE):
			sync_manager_docperms()

		grant = frappe.db.get_value(
			"Custom DocPerm",
			{"parent": doctype, "role": _ROLE, "permlevel": 0, "if_owner": 0},
			["read", "write", "create", "delete"],
			as_dict=True,
		)
		self.assertTrue(grant, "the owner-only rule must not swallow the permlevel-0 grant")
		self.assertTrue(grant.read and grant.write and grant.create and grant.delete)
		# The grant must land on the row it created, not on the admin's owner-only rule.
		self.assertFalse(frappe.db.get_value("Custom DocPerm", owner_rule, "create"))


class TestRevokeUndeclaredDocperms(ManagerRoleTestCase):
	"""Grants outlive the app that asked for them.

	manager_roles() reads the hook, which resolves from active apps, so uninstalling or
	disabling the declaring app makes every endpoint reject the role while its Custom
	DocPerm rows keep the desk form and /api/resource open on the mapping doctypes —
	where a delete evicts rule-managed members from a live Raven channel.
	"""

	def setUp(self):
		self.ensure_role()

	def test_grants_stay_while_an_active_app_declares_the_role(self):
		with declared_roles(_ROLE):
			sync_manager_docperms()
			revoke_undeclared_manager_docperms()
			self.assertTrue(self.docperm_rows())

	def test_grants_go_when_no_active_app_declares_the_role(self):
		with declared_roles(_ROLE):
			sync_manager_docperms()
			self.assertTrue(self.docperm_rows())
		with declared_roles():
			revoke_undeclared_manager_docperms("lms")
		self.assertFalse(self.docperm_rows())

	def test_the_doctypes_own_roles_are_left_alone(self):
		# add_permission() copies the doctype's JSON perms into Custom DocPerm on first
		# touch. Those rows are not ours and must survive the revoke.
		with declared_roles(_ROLE):
			sync_manager_docperms()
		with declared_roles():
			revoke_undeclared_manager_docperms()
		for doctype in MANAGED_DOCTYPES:
			self.assertTrue(
				frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": "System Manager"}),
				f"{doctype} must keep the System Manager row its JSON declares",
			)

	def test_uninstall_is_wired_to_the_revoke_and_migrate_is_not(self):
		# Migrate deliberately does not revoke: nothing marks a grant as this app's,
		# so a migrate pass would silently delete a permission a human added by hand.
		from raven_integration import hooks

		path = "raven_integration.permissions.revoke_undeclared_manager_docperms"
		self.assertIn(path, hooks.after_app_uninstall)
		self.assertNotIn(path, hooks.after_migrate)


class TestManagerCanManageWorkspaces(ManagerRoleTestCase):
	"""The DocPerm half of the fix.

	Widening only_for is not enough: create/rename/delete go through the framework
	permission check, so without DocPerms these endpoints fail after the gate.
	"""

	def setUp(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")
		self.ensure_role()
		self.manager = self.make_user("rv-e2e-manager@example.com", "E2EManager", with_role=True)

	def drop_raven_workspaces(self, suffix: str) -> None:
		for name in frappe.get_all(
			"Raven Workspace", filters={"name": ("like", f"%{suffix}%")}, pluck="name"
		):
			frappe.delete_doc(
				"Raven Workspace", name, force=True, ignore_missing=True, ignore_permissions=True
			)

	def test_manager_can_create_rename_and_delete_a_workspace(self):
		from raven_integration.api import (
			create_channel,
			create_workspace,
			delete_workspace,
			set_workspace_label,
		)

		# The preconditions that make this test worth running: the manager is a Website
		# User with no Raven presence, exactly like an LMS Moderator.
		self.assertEqual(frappe.db.get_value("User", self.manager.name, "user_type"), "Website User")
		self.assertFalse(frappe.db.exists("Raven User", {"user": self.manager.name}))

		suffix = frappe.generate_hash(length=6)
		# The rename below renames the backing Raven Workspace too, so clean up by
		# suffix rather than by the name it was created with.
		self.addCleanup(self.drop_raven_workspaces, suffix)

		with declared_roles(_ROLE):
			sync_manager_docperms()
			frappe.set_user(self.manager.name)

			name = create_workspace(label=f"E2E WS {suffix}", type="Private")
			self.assertTrue(frappe.db.get_value("Raven Workspace Mapping", name, "raven_workspace"))
			# Raven makes the creator the workspace admin, and that member row links to
			# Raven User — so the create has to have provisioned the manager in Raven.
			self.assertTrue(frappe.db.exists("Raven User", {"user": self.manager.name}))

			channel = create_channel(workspace=name, label=f"E2E CH {suffix}", type="Private")
			self.assertTrue(frappe.db.get_value("Raven Channel Mapping", channel, "raven_channel"))

			# Renames the mapping (write on our doctype) and the backing Raven
			# Workspace (ignore_permissions — a manager is not a Raven admin).
			renamed = set_workspace_label(name=name, label=f"E2E WS Renamed {suffix}")["name"]
			self.assertEqual(
				frappe.db.get_value("Raven Workspace Mapping", renamed, "workspace_label"),
				f"E2E WS Renamed {suffix}",
			)

			delete_workspace(name=renamed)
			self.assertFalse(frappe.db.exists("Raven Workspace Mapping", renamed))
		# The mapping is gone; the Raven workspace it managed is deliberately left.
		self.assertTrue(frappe.db.exists("Raven Workspace", f"E2E WS Renamed {suffix}"))

	def test_roleless_user_still_cannot_create_a_workspace(self):
		from raven_integration.api import create_workspace

		plain = self.make_user("rv-e2e-plain@example.com", "E2EPlain")
		with declared_roles(_ROLE):
			sync_manager_docperms()
			frappe.set_user(plain.name)
			with self.assertRaises(frappe.PermissionError):
				create_workspace(label="E2E Should Not Exist", type="Private")
