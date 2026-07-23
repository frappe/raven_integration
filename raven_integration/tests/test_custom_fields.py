import frappe
from frappe.tests.utils import FrappeTestCase

from raven_integration.custom_fields import (
	ensure_added_by_rule_field,
	remove_added_by_rule_field,
)


class TestCustomFields(FrappeTestCase):
	def setUp(self):
		if "raven" not in frappe.get_installed_apps():
			self.skipTest("Raven not installed")

	def test_creates_field_on_raven_channel_member(self):
		ensure_added_by_rule_field()
		self.assertTrue(frappe.db.has_column("Raven Channel Member", "added_by_rule"))

	def test_creates_field_on_raven_workspace_member(self):
		ensure_added_by_rule_field()
		self.assertTrue(frappe.db.has_column("Raven Workspace Member", "added_by_rule"))

	def test_create_is_idempotent(self):
		ensure_added_by_rule_field()
		ensure_added_by_rule_field()  # second call must not raise
		self.assertTrue(frappe.db.has_column("Raven Channel Member", "added_by_rule"))

	def test_delete_removes_fields(self):
		ensure_added_by_rule_field()
		remove_added_by_rule_field()
		self.assertFalse(frappe.db.exists("Custom Field", "Raven Channel Member-added_by_rule"))
		ensure_added_by_rule_field()  # re-create so subsequent tests have it
