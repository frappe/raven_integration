import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import escape_html


class RavenChannelMapping(Document):
	def validate(self) -> None:
		from raven_integration.engine import parse_tree, validate_member_rules

		# Surface the friendly "field is required" error before the custom rule
		# checks, which would otherwise flag two blank rows as duplicates.
		self._validate_mandatory()
		validate_member_rules(parse_tree(self.member_rules_json))
		self.validate_channel_is_adoptable()

	def validate_channel_is_adoptable(self) -> None:
		"""The two checks list_unmapped_channels applies to decide what it offers.

		Here rather than in link_channel because the endpoint is not the only way to a
		mapping: a manager role holds write DocPerm on this doctype so that the
		endpoint's own insert can go through the permission check, and that same grant
		opens /api/resource and frappe.client.set_value, neither of which passes the
		endpoint. read_only on the field is a client-side hint the framework does not
		enforce on save, so the pair has to be checked where every write runs it.

		Without them a direct message can be adopted, and rule-matched strangers are
		inserted into a two-person conversation; a channel from another Raven
		workspace joins its members to this mapping's workspace while on_trash evicts
		them against the one the channel actually lives in.

		Only when one of the two fields moves. An ordinary save — a relabel, a type
		change, a rule edit — leaves the pair alone and pays no query. A mapping with
		no channel behind it, and a stale one whose channel Raven has deleted, have
		nothing to check.
		"""
		if not self.raven_channel:
			return
		if not (self.has_value_changed("raven_channel") or self.has_value_changed("workspace")):
			return
		ch = frappe.db.get_value(
			"Raven Channel",
			self.raven_channel,
			["channel_name", "workspace", "is_direct_message", "is_thread"],
			as_dict=True,
		)
		if not ch:
			return
		if ch.is_direct_message or ch.is_thread:
			frappe.throw(
				title=_("This channel cannot be managed"),
				msg=_(
					"<b>{0}</b> is a direct message or a thread, and membership rules would "
					"add people to a private conversation. Pick a regular channel from the list."
				).format(escape_html(ch.channel_name or self.raven_channel)),
			)
		raven_workspace = frappe.db.get_value("Raven Workspace Mapping", self.workspace, "raven_workspace")
		if not raven_workspace or ch.workspace != raven_workspace:
			frappe.throw(
				title=_("Channel is in another workspace"),
				msg=_(
					"<b>{0}</b> lives in Raven workspace <b>{1}</b>, not in <b>{2}</b>. "
					"Open that workspace and adopt the channel there."
				).format(
					escape_html(ch.channel_name or self.raven_channel),
					escape_html(ch.workspace or _("none")),
					escape_html(raven_workspace or _("none")),
				),
			)

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
