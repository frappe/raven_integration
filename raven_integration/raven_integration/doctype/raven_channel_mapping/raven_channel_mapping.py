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
