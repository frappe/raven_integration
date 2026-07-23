from frappe.model.document import Document


class RavenMembershipRule(Document):
	# No validate() here: Frappe never runs a child row's validate() on a parent save.
	# Rule validation lives on Raven Workspace Mapping / Raven Channel Mapping.
	pass
