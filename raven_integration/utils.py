from __future__ import annotations

import frappe

def raven_installed() -> bool:
	"""True if the Raven app is installed on this site."""
	return "raven" in frappe.get_installed_apps()
