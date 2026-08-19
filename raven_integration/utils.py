from __future__ import annotations

import frappe


def raven_installed() -> bool:
	"""True if the Raven app is active on this site.

	Active, not merely installed: `bench disable-app raven` leaves the app in
	get_installed_apps() with its tables intact, and frappe itself switches to
	get_active_apps() to resolve hooks and to skip a disabled app's scheduled jobs.
	Anything else has the sweep adding and removing member rows in an app the site
	has turned off.
	"""
	return "raven" in frappe.get_active_apps()
