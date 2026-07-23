import frappe
from frappe.model.utils.rename_field import rename_field

MAPPINGS = ("Raven Workspace Mapping", "Raven Channel Mapping")


def execute():
	"""Flip the mapping kill-switch from `disabled` to `enabled`.

	Runs post_model_sync, so the JSON has already added `enabled` (default 1) and
	orphaned the `disabled` column — Frappe never drops columns. `enabled` is
	derived from that untouched column every time, so a re-run reproduces the same
	result rather than double-flipping.
	"""
	for doctype in MAPPINGS:
		if not frappe.db.table_exists(doctype):
			continue
		if "disabled" not in frappe.db.get_table_columns(doctype):
			continue

		rename_field(doctype, "disabled", "enabled")

		mapping = frappe.qb.DocType(doctype)
		frappe.qb.update(mapping).set(mapping.enabled, 0).where(mapping.disabled == 1).run()
		frappe.qb.update(mapping).set(mapping.enabled, 1).where(mapping.disabled == 0).run()
		frappe.db.commit()
