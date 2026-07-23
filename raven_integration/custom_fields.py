import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from raven_integration.utils import raven_installed

_FIELD = {
	"fieldname": "added_by_rule",
	"fieldtype": "Check",
	"label": "Added by Rule",
	"default": 0,
	"hidden": 1,
	"no_copy": 1,
	"read_only": 1,
	"description": "Set by Raven Integration. Sync only removes members with this flag = 1.",
}


def ensure_added_by_rule_field() -> None:
	if not raven_installed():
		return
	create_custom_fields(
		{
			"Raven Channel Member": [_FIELD],
			"Raven Workspace Member": [_FIELD],
		},
		update=True,
	)
	# Index for the diff query: per specs/performance.md. add_index is already
	# idempotent (has_index check + ADD INDEX IF NOT EXISTS), so a raise here is a
	# real failure and must not be swallowed.
	for parent in ("Raven Channel Member", "Raven Workspace Member"):
		frappe.db.add_index(parent, ["added_by_rule"])


def remove_added_by_rule_field() -> None:
	# Deleting a Custom Field does not drop its column: Frappe's Custom Field
	# on_trash only clears property setters, layouts and caches. The added_by_rule
	# values survive uninstall and are re-adopted by the existing column when
	# ensure_added_by_rule_field runs again, so provenance is preserved. Do not
	# "complete" this by dropping the columns — that would be the destructive path.
	for parent in ("Raven Channel Member", "Raven Workspace Member"):
		name = f"{parent}-added_by_rule"
		if frappe.db.exists("Custom Field", name):
			frappe.delete_doc("Custom Field", name, ignore_permissions=True)
