from __future__ import annotations

import frappe

from raven_integration import events
from raven_integration.utils import raven_installed


def reconcile_all() -> dict:
	"""Nightly reconcile: detect dangling links, then run the shared resync sweep."""
	if not events.is_active():
		return {"skipped": True, "reason": "inactive"}

	# Mark dangling links stale FIRST so newly-stale records are excluded from
	# the sweep below in the same run.
	detect_dangling_links()

	return events.resync_all()


def detect_dangling_links() -> dict:
	"""Mark mappings stale when their Raven counterpart no longer exists.

	The on_trash hook is the primary signal, but it is fail-safe and is bypassed
	entirely by frappe.db.delete, bulk deletes and app uninstalls, so this sweep is
	the backstop. It sets `stale`, never clears a channel's `enabled`: that flag is
	the user's own choice to stop syncing, and recreating a Raven record clears only
	`stale` — so disabling here would leave a recreated mapping silently unsynced.
	"""
	if not raven_installed():
		return {"skipped": True}

	flagged = 0

	# Same workspace/channel table the on_trash hook uses, so this backstop can
	# never drift from the primary signal.
	for raven_doctype, (mapping_doctype, link_field) in events._RAVEN_BACKED_MAPPINGS.items():
		noun = raven_doctype.removeprefix("Raven ").lower()
		for mapping in frappe.get_all(mapping_doctype, filters={"stale": 0}, fields=["name", link_field]):
			link = mapping.get(link_field)
			if not link or frappe.db.exists(raven_doctype, link):
				continue
			frappe.db.set_value(mapping_doctype, mapping.name, "stale", 1, update_modified=False)
			frappe.log_error(
				title=f"Dangling link: {raven_doctype} {link!r} no longer exists",
				message=(
					f"{mapping_doctype} {mapping.name!r} references {raven_doctype} "
					f"{link!r} which was deleted. "
					f"{mapping_doctype} {mapping.name!r} has been marked stale; "
					f"recreate the Raven {noun} from Settings to resume syncing."
				),
			)
			flagged += 1

	return {"flagged_stale": flagged}
