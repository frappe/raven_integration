import frappe
from frappe.model.document import Document


class RavenChannelMapping(Document):
	def validate(self) -> None:
		from raven_integration.engine import parse_tree, validate_member_rules

		# Surface the friendly "field is required" error before the custom rule
		# checks, which would otherwise flag two blank rows as duplicates.
		self._validate_mandatory()
		validate_member_rules(parse_tree(self.member_rules_json))

	def before_insert(self) -> None:
		if self.flags.get("skip_raven_create"):
			return
		from raven_integration.sync_service import create_raven_channel_for

		create_raven_channel_for(self)

	def on_update(self) -> None:
		# Carry a visibility change onto the backing Raven Channel. before_insert
		# already created it with the right type, so skip the insert pass. Inline
		# db.set_value edits bypass this hook, so the type endpoint saves through the doc.
		if self.flags.in_insert or not self.has_value_changed("channel_type"):
			return
		from raven_integration.sync_service import push_channel_type_to_raven

		push_channel_type_to_raven(self)

	def on_trash(self) -> None:
		from raven_integration.sync_service import evict_channel_rule_managed_members

		# In on_trash, so it runs inside the delete's own transaction: a channel
		# that fails to delete must not have lost its members on the way. And
		# before the row goes, because both ids the eviction needs are only
		# reachable through it — the Raven channel off this mapping, the Raven
		# workspace off its parent. The workspace path is ordered the same way.
		raven_workspace = frappe.db.get_value("Raven Workspace Mapping", self.workspace, "raven_workspace")
		evict_channel_rule_managed_members(raven_workspace, self.raven_channel)
