from __future__ import annotations

import frappe

# Doctype -> the permission types a manager needs on it. The API's own writes go
# through the framework permission check (insert/save/rename/delete are not
# ignore_permissions), so a role that passes require_manager() still needs DocPerms
# or the endpoint fails half way through.
MANAGED_DOCTYPES: dict[str, tuple[str, ...]] = {
	"Raven Membership Settings": ("read", "write"),
	"Raven Workspace Mapping": ("read", "write", "create", "delete", "report", "export"),
	"Raven Channel Mapping": ("read", "write", "create", "delete", "report", "export"),
}

_HOOK = "raven_integration_manager_roles"


def manager_roles() -> list[str]:
	"""Roles allowed to manage the integration.

	System Manager always, plus whatever a host app declares via the
	`raven_integration_manager_roles` hook. This app knows nothing about its host's
	role names — LMS contributes "Moderator" the same way it contributes rule types.
	"""
	roles = ["System Manager"]
	for role in frappe.get_hooks(_HOOK) or []:
		if role and role not in roles:
			roles.append(role)
	return roles


def require_manager() -> None:
	frappe.only_for(manager_roles())


def _declared_roles() -> list[str]:
	"""manager_roles() minus System Manager, which the doctype JSONs already grant."""
	return [role for role in manager_roles() if role != "System Manager"]


def _doctype_own_roles() -> set[str]:
	"""Roles the managed doctypes' own DocPerms grant.

	add_permission() copies these into Custom DocPerm the first time it touches a
	doctype, so they sit alongside our grants and must never be dropped with them.
	"""
	roles: set[str] = set()
	for doctype in MANAGED_DOCTYPES:
		roles.update(frappe.get_all("DocPerm", filters={"parent": doctype}, pluck="role"))
	return roles


def _granted_roles() -> set[str]:
	"""Roles holding a Custom DocPerm on a managed doctype because this app put it there.

	Read off the rows rather than off the hook: the grant that most needs dropping
	belongs to a role whose declaring app is already gone, and get_hooks resolves
	active apps only, so by then nothing names it any more.
	"""
	granted: set[str] = set()
	for doctype in MANAGED_DOCTYPES:
		granted.update(frappe.get_all("Custom DocPerm", filters={"parent": doctype}, pluck="role"))
	return granted - _doctype_own_roles()


def _drop_docperms(roles: set[str]) -> None:
	if not roles:
		return
	names = sorted(roles)
	for doctype in MANAGED_DOCTYPES:
		frappe.db.delete("Custom DocPerm", {"parent": doctype, "role": ("in", names)})
		# db.delete writes no document, so nothing invalidates the doctype's cached meta.
		frappe.clear_cache(doctype=doctype)


def _set_permission_types(row: str, ptypes: tuple[str, ...]) -> None:
	"""Turn on the ptypes a manager needs, addressing the row by name.

	Not update_permission_property(): it looks a row up by (parent, role, permlevel)
	without if_owner, so with an "Apply Only to Owner" rule in place for the same role
	it can write the grant onto that row instead of the one just created.
	"""
	perm = frappe.get_doc("Custom DocPerm", row)
	perm.update({ptype: 1 for ptype in ptypes})
	perm.save(ignore_permissions=True)


def sync_manager_docperms(app_name: str | None = None) -> None:
	"""Grant every hook-declared manager role the DocPerms the API's writes need.

	Runs from after_install, after_migrate and after_app_install. The last one is not
	redundant: a host app may be installed after this one, and the hook only starts
	naming its role at that point.

	``app_name`` is what the app hooks pass; it is ignored because any app's hooks.py
	may declare a manager role.

	Note that add_permission() copies the doctype's own DocPerm rows into Custom
	DocPerm the first time it touches a doctype, after which the JSON permissions
	stop taking effect for it. That is the framework's only way to grant a role
	declared by another app, and the same trade-off LMS makes for User and Event.
	"""
	from frappe.permissions import add_permission

	for role in _declared_roles():
		if not frappe.db.exists("Role", role):
			continue
		for doctype, ptypes in MANAGED_DOCTYPES.items():
			# if_owner, matching what add_permission itself probes for. Without it an
			# admin's "Apply Only to Owner" rule for this role answers the probe, the
			# grant is skipped, and require_manager() then passes for a role whose
			# insert() raises PermissionError half way through the endpoint.
			if frappe.db.exists(
				"Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0, "if_owner": 0}
			):
				continue
			row = add_permission(doctype, role, 0)
			if row:
				_set_permission_types(row, ptypes)


def revoke_undeclared_manager_docperms(app_name: str | None = None) -> None:
	"""Drop the grants of every role no active app declares any more.

	Runs from after_app_uninstall only. Uninstalling the app that named a manager role
	takes the role out of manager_roles(), so every endpoint starts rejecting it, but
	the Custom DocPerm rows survive and keep the desk form and /api/resource open on the
	mapping doctypes, where a delete evicts rule-managed members from a live Raven
	channel.

	The hook is the only source of truth for who manages this integration, so a grant on
	a managed doctype that no hook backs goes even if a human added it by hand. That is
	why this is not wired to after_migrate: nothing marks a grant as ours, so a migrate
	pass would delete a hand-added permission with no uninstall to explain it. The cost
	is a host app that was `disable-app`'d rather than uninstalled — that fires no hook
	of ours, so its grants persist until it is uninstalled for real.

	``app_name`` is ignored: the role could have been declared by any app, not only the
	one being uninstalled.

	Commits its own work. ``after_app_uninstall`` is the one hook frappe runs *after*
	remove_app's last commit, and `bench uninstall-app` ends in frappe.destroy() —
	db.close() with nothing committed in between — so these deletes are rolled back on
	the way out and the stale grants survive the uninstall they exist to clean up.
	(`remove_manager_docperms` on `before_uninstall` needs no such commit: it runs
	before _delete_modules, whose DDL commits the transaction it sits in.)
	"""
	dropped = _granted_roles() - set(manager_roles())
	if not dropped:
		return
	_drop_docperms(dropped)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit — see the docstring


def remove_manager_docperms() -> None:
	"""Drop every grant sync_manager_docperms() created.

	Called from before_uninstall so uninstalling this app does not leave grants
	pointing at doctypes that are about to disappear. Computed from the rows and not
	from _declared_roles(): the host app may have been uninstalled first, and its role
	is then the one row nothing else will ever clean up.
	"""
	_drop_docperms(_granted_roles())
