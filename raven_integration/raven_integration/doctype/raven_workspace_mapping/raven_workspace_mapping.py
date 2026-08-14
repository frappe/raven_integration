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
		from raven_integration.sync_service import evict_rule_managed_members

		# A Raven Channel Mapping links back to its workspace, so the framework
		# blocks the workspace delete while channels exist. Cascade-delete the
		# child channel mappings first to clear the link.
		channels = frappe.get_all(
			"Raven Channel Mapping", filters={"workspace": self.name}, fields=["name", "raven_channel"]
		)
		# Before the cascade, not after: the Raven ids these rows are addressed by
		# live on the channel mappings, and a deleted mapping cannot say which
		# Raven channel it managed. Inside the delete's own transaction too, so a
		# workspace that fails to delete does not lose its members on the way.
		evict_rule_managed_members(self.raven_workspace, [c.raven_channel for c in channels])
		# Each cascaded delete evicts its own channel too. By now there is nothing
		# left for it to find, so it is a no-op rather than a second withdrawal —
		# but doing the whole workspace in one pass first is what makes the
		# workspace's own member rows go unconditionally, which a per-channel
		# eviction, deriving them from the channels, would not do.
		for channel in channels:
			frappe.delete_doc("Raven Channel Mapping", channel.name)
