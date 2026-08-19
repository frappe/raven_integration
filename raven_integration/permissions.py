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
	from frappe.permissions import add_permission, update_permission_property

	for role in _declared_roles():
		if not frappe.db.exists("Role", role):
			continue
		for doctype, ptypes in MANAGED_DOCTYPES.items():
			if frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0}):
				continue
			add_permission(doctype, role, 0)
			for ptype in ptypes:
				update_permission_property(doctype, role, 0, ptype, 1)


def remove_manager_docperms() -> None:
	"""Drop the Custom DocPerm rows sync_manager_docperms() created.

	Called from before_uninstall so uninstalling this app does not leave grants
	pointing at doctypes that are about to disappear.
	"""
	roles = _declared_roles()
	if not roles:
		return
	for doctype in MANAGED_DOCTYPES:
		frappe.db.delete("Custom DocPerm", {"parent": doctype, "role": ("in", roles)})
