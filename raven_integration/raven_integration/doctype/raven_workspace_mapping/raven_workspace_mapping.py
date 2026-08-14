import frappe
from frappe.model.document import Document


class RavenWorkspaceMapping(Document):
	def validate(self) -> None:
		# Membership rules live on Raven Channel Mapping only, so a workspace has
		# nothing rule-shaped to validate: its membership is derived from whoever is
		# in at least one of its channels.
		self._validate_mandatory()

	def before_insert(self) -> None:
		if self.flags.get("skip_raven_create"):
			return
		from raven_integration.sync_service import create_raven_workspace_for

		create_raven_workspace_for(self)

	def on_update(self) -> None:
		# Carry a visibility change onto the backing Raven Workspace. before_insert
		# already created it with the right type, so skip the insert pass. Inline
		# db.set_value edits bypass this hook, so the type endpoint saves through the doc.
		if self.flags.in_insert or not self.has_value_changed("workspace_type"):
			return
		from raven_integration.sync_service import push_workspace_type_to_raven

		push_workspace_type_to_raven(self)

	def on_trash(self) -> None:
		# A Raven Channel Mapping links back to its workspace, so the framework
		# blocks the workspace delete while channels exist. Cascade-delete the
		# child channel mappings first to clear the link.
		for channel in frappe.get_all(
			"Raven Channel Mapping", filters={"workspace": self.name}, pluck="name"
		):
			frappe.delete_doc("Raven Channel Mapping", channel)
