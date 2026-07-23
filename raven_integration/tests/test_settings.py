import frappe
from frappe.tests.utils import FrappeTestCase


class TestRavenMembershipSettings(FrappeTestCase):
	def test_settings_single_exists(self):
		s = frappe.get_single("Raven Membership Settings")
		self.assertEqual(s.doctype, "Raven Membership Settings")
