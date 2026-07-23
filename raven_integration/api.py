from typing import Any

import frappe
from frappe import _
from frappe.utils import escape_html

_VALID_WS_TYPES = {"Public", "Private"}
_VALID_CH_TYPES = {"Public", "Private", "Open"}
_VALID_COMBINATORS = {"Any (OR)", "All (AND)"}
# The Select options declared on Raven Membership Rule.status.
_VALID_RULE_STATUSES = {"Active", "Paused"}

# Mapping doctype -> its free-text label field. Both autoname as
# `format:<PREFIX>-{label}`, so the docname is derived from the label.
_MAPPING_LABEL_FIELDS = {
	"Raven Workspace Mapping": "workspace_label",
	"Raven Channel Mapping": "channel_label",
}


def _require_system_manager() -> None:
	frappe.only_for(["System Manager"])


def _str(value: Any, field: str) -> str:
	if not isinstance(value, str):
		frappe.throw(
			title=_("Invalid value for {0}").format(field),
			msg=_(
				"<b>{0}</b> must be text, but the request sent {1}. Reload the page and try again."
			).format(field, type(value).__name__),
		)
	return value


def _bool(value: Any, field: str) -> bool:
	if not isinstance(value, bool):
		frappe.throw(
			title=_("Invalid value for {0}").format(field),
			msg=_(
				"<b>{0}</b> must be true or false, but the request sent {1}. "
				"Reload the page and try again."
			).format(field, type(value).__name__),
		)
	return value


def _choice(value: Any, field: str, allowed: set[str], title: str) -> str:
	value = _str(value, field)
	if value not in allowed:
		frappe.throw(
			title=title,
			msg=_(
				"<b>{0}</b> must be one of {1}, but the request sent {2}. "
				"Pick one of the listed values and try again."
			).format(field, ", ".join(sorted(allowed)), escape_html(value)),
		)
	return value


def _combinator(value: "str | None") -> str:
	if value is None:
		return "Any (OR)"
	return _choice(value, "combinator", _VALID_COMBINATORS, _("Invalid rule combinator"))


def _rules_list(value: Any, require_label: bool = True) -> list[dict]:
	"""Validate a rules payload. ``require_label`` is off for the read-only preview
	path: a name identifies a rule to the user and is required to *save* one, but it
	has no bearing on who a rule matches, and a stored rule may still be unnamed
	(labels the old backend generated are blanked by the migration patch)."""
	if not isinstance(value, list):
		frappe.throw(
			title=_("Invalid member rules"),
			msg=_(
				"<b>Member Rules</b> must be a list of rules, but the request sent {0}. "
				"Reload the page and try again."
			).format(type(value).__name__),
		)
	for i, r in enumerate(value):
		if not isinstance(r, dict):
			frappe.throw(
				title=_("Invalid member rule"),
				msg=_(
					"Row #{0} of <b>Member Rules</b> must be a rule, but the request sent {1}. "
					"Reload the page and try again."
				).format(i + 1, type(r).__name__),
			)
		if not require_label:
			continue
		# A rule name is the only thing that tells two rules of the same type apart,
		# so it is required rather than defaulted. Rejecting it here — before any
		# field on the mapping is touched — gives the user a message naming the row,
		# instead of the MandatoryError doc.save() would raise further down.
		label = r.get("label")
		if not isinstance(label, str) or not label.strip():
			frappe.throw(
				title=_("Rule name is required"),
				msg=_(
					"Row #{0} of <b>Member Rules</b> has no name. A name is what tells two "
					"rules of the same type apart. Name the rule, then save again."
				).format(i + 1),
			)
		r["label"] = label.strip()
	return value


def _require_mapping(doctype: str, name: str) -> None:
	"""Fail loudly on an unknown mapping name.

	`db.set_value` on a row that does not exist is a silent no-op, so without this
	the UI reports success for a change that never landed."""
	if not frappe.db.exists(doctype, name):
		frappe.throw(
			title=_("{0} not found").format(_(doctype)),
			msg=_(
				"No {0} named <b>{1}</b> exists. It may have been deleted — "
				"reload the page and try again."
			).format(_(doctype), escape_html(name)),
			exc=frappe.DoesNotExistError,
		)


def _require_stale(doctype: str, name: str) -> None:
	"""Guard the recreate endpoints: only a mapping whose Raven record is gone may
	be given a new one, or a live Raven workspace/channel would be orphaned."""
	_require_mapping(doctype, name)
	if not frappe.db.get_value(doctype, name, "stale"):
		frappe.throw(
			title=_("Nothing to recreate"),
			msg=_(
				"<b>{0}</b> is still linked to a Raven record that exists, so recreating it "
				"would abandon that record. Reload the page — it may already have been "
				"recreated in another tab."
			).format(escape_html(name)),
		)


def _require_rule_of(doctype: str, name: str, rule: str) -> None:
	"""Fail loudly unless ``rule`` is a member-rule row of that exact mapping.

	Both parent and parenttype are checked. Today the two mapping doctypes autoname
	with different prefixes, so a shared docname cannot occur — but that is a
	property of the naming rules, not of this check, and `Raven Membership Rule`
	rows are only unique within their own table."""
	row = frappe.db.get_value(
		"Raven Membership Rule", rule, ["parent", "parenttype"], as_dict=True
	)
	if not row or row.parent != name or row.parenttype != doctype:
		frappe.throw(
			title=_("Rule not found"),
			msg=_(
				"No member rule <b>{0}</b> belongs to <b>{1}</b>. It may have been deleted "
				"or moved — reload the page and try again."
			).format(escape_html(rule), escape_html(name)),
			exc=frappe.DoesNotExistError,
		)


def _set_rule_status(doctype: str, name: str, rule: str, status: str) -> dict:
	"""Shared body of set_workspace_rule_status / set_channel_rule_status."""
	name = _str(name, "name")
	rule = _str(rule, "rule")
	status_ = _choice(status, "status", _VALID_RULE_STATUSES, _("Invalid rule status"))
	_require_mapping(doctype, name)
	_require_rule_of(doctype, name, rule)
	# db.set_value on the child row — deliberately unlike set_*_combinator, which
	# saves the whole doc to fire its hooks. A full save revalidates every rule on
	# the mapping and rewrites the child table wholesale, and this endpoint exists
	# precisely to reach a rule a full save cannot: a Paused, unnamed one, whose
	# card the UI freezes and whose save path the name check rejects. Writing the
	# one field also keeps the status change clear of the full-list-replace
	# deletion bug. _schedule_resync() supplies the membership refresh instead.
	frappe.db.set_value("Raven Membership Rule", rule, "status", status_)
	_schedule_resync()
	return {"status": status_}


def _mapping_docname(doctype: str, label: str) -> str:
	"""The docname the doctype's `format:` autoname rule produces for this label.

	Uses the framework's own formatter rather than hardcoding the prefix, so the
	autoname string in the doctype JSON stays the single source of truth."""
	from frappe.model.naming import _format_autoname

	probe = frappe.new_doc(doctype)
	probe.set(_MAPPING_LABEL_FIELDS[doctype], label)
	# validate_name strips before storing, so strip here too or the name we compute
	# (and return to the caller) differs from the one actually written.
	return _format_autoname(frappe.get_meta(doctype).autoname, probe).strip()


def _next_default_label(doctype: str, base: str, skip: "set[str] | None" = None) -> str:
	"""Smallest 'base N' (N starting at 1) whose *docname* is still free.

	Uniqueness is enforced on the docname, not on the label field — the label
	field carries no unique constraint. Probing the label field instead deadlocks
	as soon as one label has been edited: the docname keeps the original label,
	the probe sees that label as free, and every insert collides forever.

	``skip`` holds labels a caller already lost an insert race on, so retries make
	progress instead of re-proposing the same name."""
	taken = set(frappe.get_all(doctype, pluck="name"))
	skip = skip or set()
	n = 1
	while True:
		label = f"{base} {n}"
		if label not in skip and _mapping_docname(doctype, label) not in taken:
			return label
		n += 1


def _set_mapping_label(doctype: str, name: str, label: str) -> str:
	"""Apply a new label and rename the doc to match. Returns the (possibly new) docname.

	These doctypes autoname as `format:<PREFIX>-{label}`, and the framework only
	keeps the docname in sync for `field:` autonames. Writing the label field on
	its own therefore leaves the docname pinned to the original label, which then
	permanently blocks that default name from being allocated again."""
	new_name = _mapping_docname(doctype, label)
	if new_name != name:
		# db.exists() is case-insensitive and rename_doc allows case-only fix-ups,
		# so only an exact-cased hit is a real collision.
		if frappe.db.exists(doctype, new_name) == new_name:
			frappe.throw(
				title=_("Name already in use"),
				msg=_(
					"Another {0} is already called <b>{1}</b>. "
					"Pick a different name and try again."
				).format(_(doctype), escape_html(label)),
			)
		# force=True: these doctypes do not set allow_rename, but renaming is the
		# only way to keep the autoname-derived docname truthful.
		frappe.rename_doc(doctype, name, new_name, force=True, show_alert=False)
	frappe.db.set_value(doctype, new_name, _MAPPING_LABEL_FIELDS[doctype], label)
	return new_name


def _rename_raven_workspace(mapping_name: str, label: str) -> None:
	"""Propagate a workspace-mapping label change to its backing Raven Workspace.

	A Raven Workspace autonames from its workspace_name, so its docname *is* its
	display name — renaming the display requires frappe.rename_doc, which also
	cascades the new name into this mapping's raven_workspace Link. Skipped when the
	mapping is stale or unlinked: there is no live Raven record to rename."""
	row = frappe.db.get_value(
		"Raven Workspace Mapping", mapping_name, ["raven_workspace", "stale"], as_dict=True
	)
	if not row or row.stale or not row.raven_workspace:
		return
	if row.raven_workspace == label:
		return
	# db.exists is case-insensitive and rename_doc permits case-only fix-ups, so
	# only an exact-cased hit is a real collision.
	if frappe.db.exists("Raven Workspace", label) == label:
		frappe.throw(
			title=_("Name already in use"),
			msg=_(
				"A Raven workspace named <b>{0}</b> already exists. "
				"Pick a different name and try again."
			).format(escape_html(label)),
		)
	# Suppress the reverse after_rename handler: this rename originates from the
	# mapping, and _relabel_mapping renames the mapping itself right after. Without
	# this, the reverse handler would rename the mapping first and the caller's own
	# rename would then fail on a docname that no longer exists.
	from raven_integration.sync_service import pushing_to_raven

	with pushing_to_raven():
		frappe.rename_doc("Raven Workspace", row.raven_workspace, label, show_alert=False)


def _rename_raven_channel(mapping_name: str, label: str) -> None:
	"""Propagate a channel-mapping label change to its backing Raven Channel.

	A Raven Channel's name/id is independent of its display, so only the
	channel_name field changes; the id and this mapping's raven_channel Link stay
	valid. Raven slugifies channel_name and enforces per-workspace name uniqueness,
	so a name Raven rejects surfaces as a friendly error. Skipped when the mapping
	is stale or unlinked."""
	row = frappe.db.get_value(
		"Raven Channel Mapping", mapping_name, ["raven_channel", "stale"], as_dict=True
	)
	if not row or row.stale or not row.raven_channel:
		return
	ch = frappe.get_doc("Raven Channel", row.raven_channel)
	ch.channel_name = label
	try:
		ch.save(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError, frappe.ValidationError):
		frappe.throw(
			title=_("Name already in use"),
			msg=_(
				"Raven would not accept the channel name <b>{0}</b> — a channel with that "
				"name may already exist in this workspace. Pick a different name and try again."
			).format(escape_html(label)),
		)


def _enqueue_member_sync(doctype: str, name: str) -> None:
	"""Queue one mapping's member sync. The diff evaluates every rule against every
	user, so it always runs in the background rather than blocking the request."""
	if doctype == "Raven Channel Mapping":
		frappe.enqueue(
			"raven_integration.sync_service.sync_channel_members",
			queue="short",
			enqueue_after_commit=True,
			channel_name=name,
		)
	else:
		frappe.enqueue(
			"raven_integration.sync_service.sync_workspace_members",
			queue="short",
			enqueue_after_commit=True,
			workspace_name=name,
		)


def _create_mapping(
	doctype: str,
	base: str,
	label: "str | None",
	fields: dict,
	rule_rows: list[dict],
	combinator: "str | None",
	alloc_title: str,
	alloc_msg: str,
) -> str:
	"""Shared body of create_workspace / create_channel. ``fields`` carries the
	doctype-specific columns (type, and a channel's parent workspace); with no label,
	retries the next free default so a session losing the race still gets a row."""
	explicit = bool(label and str(label).strip())
	if explicit:
		label = _str(label, "label")

	tried: set[str] = set()
	for _attempt in range(5):
		chosen = label if explicit else _next_default_label(doctype, base, skip=tried)
		doc = frappe.new_doc(doctype)
		doc.set(_MAPPING_LABEL_FIELDS[doctype], chosen)
		doc.update(fields)
		doc.rule_combinator = _combinator(combinator)
		for r in rule_rows:
			doc.append("member_rules", r)
		try:
			doc.insert()
			return doc.name
		except frappe.DuplicateEntryError:
			if explicit:
				raise  # caller-chosen name already exists
			tried.add(chosen)
	frappe.throw(title=alloc_title, msg=alloc_msg)


def _update_mapping(
	doctype: str, name: str, label: str, fields: dict, rule_rows: list[dict], combinator: str
) -> str:
	"""Shared body of update_workspace / update_channel. Returns the (possibly new)
	docname, which changes when the label changes."""
	doc = frappe.get_doc(doctype, name)
	doc.update(fields)
	doc.rule_combinator = combinator
	doc.member_rules = []
	for r in rule_rows:
		doc.append("member_rules", r)
	doc.save()
	_schedule_resync()
	return _set_mapping_label(doctype, name, label)


def _set_mapping_enabled(doctype: str, name: str, enabled: bool) -> dict:
	"""Shared body of set_workspace_enabled / set_channel_enabled."""
	_require_mapping(doctype, name)
	frappe.db.set_value(doctype, name, "enabled", 1 if enabled else 0)
	if enabled:
		_enqueue_member_sync(doctype, name)
	return {"enabled": enabled}


def _set_mapping_type(doctype: str, field: str, name: str, type_: str) -> dict:
	"""Shared body of set_workspace_type / set_channel_type. Saves through the doc
	(not db.set_value) so the controller's on_update carries the new visibility onto
	the backing Raven workspace/channel."""
	_require_mapping(doctype, name)
	doc = frappe.get_doc(doctype, name)
	doc.set(field, type_)
	doc.save()
	return {"type": type_}


def _set_mapping_combinator(doctype: str, name: str, combinator: str) -> dict:
	"""Shared body of set_workspace_combinator / set_channel_combinator."""
	_require_mapping(doctype, name)
	# Through the doc, not db.set_value: this changes who belongs (union vs
	# intersection), and set_value fires no hooks at all.
	doc = frappe.get_doc(doctype, name)
	doc.rule_combinator = combinator
	doc.save()
	_schedule_resync()
	return {"combinator": combinator}


def _relabel_mapping(doctype: str, name: str, label: str, savepoint: str, rename_raven) -> dict:
	"""Shared body of set_workspace_label / set_channel_label. ``rename_raven`` carries
	the label to the backing Raven record — a rename for a workspace, a field write for
	a channel. Both writes share one savepoint, so neither side is left half-renamed."""
	_require_mapping(doctype, name)
	frappe.db.savepoint(savepoint)
	try:
		rename_raven(name, label)
		new_name = _set_mapping_label(doctype, name, label)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	return {"label": label, "name": new_name}


def _describe_rule(rule: dict) -> str:
	"""Human-readable 'matches' label for a rule — sourced from the provider's
	declared rule-type label, never from domain-specific fields."""
	from raven_integration.registry import list_rule_types

	try:
		decl = {rt["type"]: rt for rt in list_rule_types(rule.get("provider"))}.get(
			rule.get("rule_type")
		)
	except Exception:
		decl = None
	return (decl.get("label") if decl else None) or rule.get("rule_type") or ""


def _serialize_rule_for_ui(rule) -> dict:
	"""Flatten a member-rule row for the frontend. config is opaque to the core."""
	config = frappe.parse_json(rule.config) if rule.config else {}
	data = {
		"name": rule.name,
		"label": rule.label,
		"provider": rule.provider,
		"rule_type": rule.rule_type,
		"status": rule.status or "Active",
		"config": config,
	}
	data["matches"] = _describe_rule({"provider": rule.provider, "rule_type": rule.rule_type})
	return data


def _schedule_resync() -> None:
	"""Queue a membership resync after a change to a mapping's own rules/combinator.

	The wildcard doc_event only reacts to *provider* doctypes, so saving a mapping
	fires nothing — without this the edit sits inert until the daily sweep, and the
	members the confirmation dialog just warned about disappear at some unrelated
	later moment. Debounced and fail-safe inside notify_change().
	"""
	from raven_integration.events import notify_change

	notify_change()


@frappe.whitelist()
def is_setup() -> dict:
	"""Whether both Raven and this app are installed, plus whether the integration
	has been enabled (drives the Settings UI gate).

	System Manager only: the reply enumerates installed apps, and every action the
	Settings panel offers already requires that role."""
	_require_system_manager()
	apps = frappe.get_installed_apps()
	return {
		"raven": "raven" in apps,
		"raven_integration": "raven_integration" in apps,
		"enabled": bool(frappe.db.get_single_value("Raven Membership Settings", "enabled")),
	}


@frappe.whitelist()
def enable_integration() -> dict:
	"""Enable membership sync (one-way — there is no disable). Triggers an initial reconcile."""
	_require_system_manager()
	frappe.db.set_single_value("Raven Membership Settings", "enabled", 1)
	frappe.enqueue(
		"raven_integration.scheduler.reconcile_all", queue="long", enqueue_after_commit=True
	)
	return {"enabled": True}


@frappe.whitelist()
def list_providers() -> list:
	"""Membership providers registered by consumer apps (UI rule-type discovery)."""
	_require_system_manager()
	from raven_integration.registry import list_providers as _list

	return _list()


@frappe.whitelist()
def list_workspaces() -> list[dict]:
	"""Every Raven workspace the UI can show: managed mappings first (current order),
	then unmanaged Raven workspaces re-projected into the same row shape."""
	_require_system_manager()
	managed = frappe.get_all(
		"Raven Workspace Mapping",
		fields=[
			"name",
			"workspace_label",
			"workspace_type",
			"rule_combinator",
			"raven_workspace",
			"enabled",
			"stale",
		],
		order_by="modified desc",
	)
	for row in managed:
		row["mapped"] = True
	unmanaged = [
		{
			"mapped": False,
			"name": None,
			"workspace_label": w["workspace_name"],
			"workspace_type": w["type"],
			"rule_combinator": None,
			"raven_workspace": w["name"],
			"enabled": 1,
			"stale": 0,
		}
		for w in list_unmapped_workspaces()
	]
	return managed + unmanaged


@frappe.whitelist()
def get_workspace(name: str) -> dict:
	"""Full workspace detail for the detail view + edit dialog."""
	_require_system_manager()
	name = _str(name, "name")
	from raven_integration.engine import expected_workspace_members

	doc = frappe.get_doc("Raven Workspace Mapping", name)
	channels = frappe.get_all(
		"Raven Channel Mapping", filters={"workspace": name}, fields=["enabled"]
	)
	return {
		"name": doc.name,
		"workspace_label": doc.workspace_label,
		"workspace_type": doc.workspace_type,
		"rule_combinator": doc.rule_combinator,
		"enabled": doc.enabled,
		"stale": doc.stale,
		"raven_workspace": doc.raven_workspace,
		"creation": doc.creation,
		"member_count": len(expected_workspace_members(name, strict=False)),
		"channels_active": sum(1 for c in channels if c.enabled),
		"channels_paused": sum(1 for c in channels if not c.enabled),
		"member_rules": [_serialize_rule_for_ui(r) for r in doc.member_rules],
	}


@frappe.whitelist()
def create_workspace(
	label: "str | None" = None,
	type: str = "Private",
	rules: "list[dict] | None" = None,
	combinator: "str | None" = None,
) -> str:
	"""Create a Raven Workspace Mapping. With no label, auto-names 'Workspace N'
	(next free number) so the UI can add a row in one click."""
	_require_system_manager()
	type_ = _choice(type, "type", _VALID_WS_TYPES, _("Invalid workspace type"))
	rule_rows = _rules_list(rules) if rules else []
	return _create_mapping(
		"Raven Workspace Mapping",
		"Workspace",
		label,
		{"workspace_type": type_},
		rule_rows,
		combinator,
		_("Could not allocate a default workspace name"),
		_(
			"Another session claimed the auto-generated name on every attempt. "
			"Try again, or create the workspace with a name of your own."
		),
	)


@frappe.whitelist()
def update_workspace(
	name: str,
	label: str,
	type: str,
	rules: list[dict],
	combinator: "str | None" = None,
) -> str:
	"""Update a Raven Workspace Mapping. Returns the docname, which changes when
	the label changes (the docname is derived from the label)."""
	_require_system_manager()
	name = _str(name, "name")
	label = _str(label, "label")
	type_ = _choice(type, "type", _VALID_WS_TYPES, _("Invalid workspace type"))
	combinator_ = _combinator(combinator)
	rule_rows = _rules_list(rules)
	return _update_mapping(
		"Raven Workspace Mapping", name, label, {"workspace_type": type_}, rule_rows, combinator_
	)


@frappe.whitelist()
def delete_workspace(name: str) -> None:
	"""Delete the Raven Workspace Mapping and its child channel mappings.

	Deletes this app's own records only. The backing Raven Workspace — with its
	channels, members and conversations — is left intact and simply becomes
	unmanaged; deleting it is Raven's decision to make, not ours.
	"""
	_require_system_manager()
	name = _str(name, "name")
	frappe.get_doc("Raven Workspace Mapping", name).delete()


@frappe.whitelist()
def recreate_workspace(name: str) -> str:
	"""Give a stale Raven Workspace Mapping a fresh backing Raven Workspace."""
	_require_system_manager()
	name = _str(name, "name")
	_require_stale("Raven Workspace Mapping", name)
	from raven_integration.sync_service import create_raven_workspace_for

	doc = frappe.get_doc("Raven Workspace Mapping", name)
	create_raven_workspace_for(doc)
	# db.set_value, not doc.save(): before_insert/validate re-provision and
	# re-validate the whole mapping, and only these two fields are changing.
	frappe.db.set_value(
		"Raven Workspace Mapping",
		name,
		{"raven_workspace": doc.raven_workspace, "stale": 0},
	)
	_enqueue_member_sync("Raven Workspace Mapping", name)
	return doc.raven_workspace


@frappe.whitelist()
def set_workspace_enabled(name: str, enabled: bool) -> dict:
	"""Enable/disable membership sync for a single Raven Workspace Mapping."""
	_require_system_manager()
	name = _str(name, "name")
	enabled = _bool(enabled, "enabled")
	return _set_mapping_enabled("Raven Workspace Mapping", name, enabled)


@frappe.whitelist()
def set_workspace_type(name: str, type: str) -> dict:
	"""Change a Raven Workspace Mapping's visibility (Public/Private)."""
	_require_system_manager()
	name = _str(name, "name")
	type_ = _choice(type, "type", _VALID_WS_TYPES, _("Invalid workspace type"))
	return _set_mapping_type("Raven Workspace Mapping", "workspace_type", name, type_)


@frappe.whitelist()
def set_workspace_label(name: str, label: str) -> dict:
	"""Rename a Raven Workspace Mapping and its backing Raven Workspace (inline edit)."""
	_require_system_manager()
	name = _str(name, "name")
	label = _str(label, "label")
	return _relabel_mapping(
		"Raven Workspace Mapping", name, label, "ri_set_workspace_label", _rename_raven_workspace
	)


@frappe.whitelist()
def set_workspace_combinator(name: str, combinator: str) -> dict:
	"""Change how a Raven Workspace Mapping combines its rules (Any (OR) / All (AND))."""
	_require_system_manager()
	name = _str(name, "name")
	combinator_ = _combinator(combinator)
	return _set_mapping_combinator("Raven Workspace Mapping", name, combinator_)


@frappe.whitelist()
def set_workspace_rule_status(name: str, rule: str, status: str) -> dict:
	"""Pause/resume one member rule of a Raven Workspace Mapping."""
	_require_system_manager()
	return _set_rule_status("Raven Workspace Mapping", name, rule, status)


@frappe.whitelist()
def list_channels(workspace: str) -> list[dict]:
	"""Every channel in ``workspace`` the UI can show: managed channel mappings first
	(current order), then unmanaged Raven channels in the backing workspace."""
	_require_system_manager()
	workspace = _str(workspace, "workspace")
	managed = frappe.get_all(
		"Raven Channel Mapping",
		filters={"workspace": workspace},
		fields=[
			"name",
			"channel_label",
			"channel_type",
			"rule_combinator",
			"raven_channel",
			"enabled",
			"stale",
		],
		order_by="modified desc",
	)
	for row in managed:
		row["mapped"] = True
	# No backing Raven workspace to scan (unknown/unlinked/stale parent) → managed only.
	unmapped_rows = (
		list_unmapped_channels(workspace)
		if frappe.db.exists("Raven Workspace Mapping", workspace)
		else []
	)
	unmanaged = [
		{
			"mapped": False,
			"name": None,
			"channel_label": c["channel_name"],
			"channel_type": c["type"],
			"rule_combinator": None,
			"raven_channel": c["name"],
			"enabled": 1,
			"stale": 0,
		}
		for c in unmapped_rows
	]
	return managed + unmanaged


@frappe.whitelist()
def get_channel(name: str) -> dict:
	"""Full channel detail incl. member rules flattened for the rules panel."""
	_require_system_manager()
	name = _str(name, "name")
	from raven_integration.engine import expected_channel_members

	doc = frappe.get_doc("Raven Channel Mapping", name)
	return {
		"name": doc.name,
		"channel_label": doc.channel_label,
		"workspace": doc.workspace,
		"channel_type": doc.channel_type,
		"rule_combinator": doc.rule_combinator,
		"enabled": doc.enabled,
		"stale": doc.stale,
		"raven_channel": doc.raven_channel,
		"member_count": len(expected_channel_members(name, strict=False)),
		"member_rules": [_serialize_rule_for_ui(r) for r in doc.member_rules],
	}


@frappe.whitelist()
def create_channel(
	workspace: str,
	label: "str | None" = None,
	type: str = "Private",
	rules: "list[dict] | None" = None,
	combinator: "str | None" = None,
) -> str:
	"""Create a Raven Channel Mapping. With no label, auto-names 'Channel N'
	(next free number) so the UI can add a row in one click."""
	_require_system_manager()
	workspace = _str(workspace, "workspace")
	type_ = _choice(type, "type", _VALID_CH_TYPES, _("Invalid channel type"))
	if not frappe.db.exists("Raven Workspace Mapping", workspace):
		frappe.throw(
			title=_("Workspace not found"),
			msg=_(
				"No Raven Workspace Mapping named <b>{0}</b> exists. "
				"Reload the page and pick a workspace from the list."
			).format(escape_html(workspace)),
			exc=frappe.DoesNotExistError,
		)
	rule_rows = _rules_list(rules) if rules else []
	return _create_mapping(
		"Raven Channel Mapping",
		"Channel",
		label,
		{"workspace": workspace, "channel_type": type_},
		rule_rows,
		combinator,
		_("Could not allocate a default channel name"),
		_(
			"Another session claimed the auto-generated name on every attempt. "
			"Try again, or create the channel with a name of your own."
		),
	)


@frappe.whitelist()
def update_channel(
	name: str,
	label: str,
	type: str,
	rules: list[dict],
	combinator: "str | None" = None,
) -> str:
	"""Update a Raven Channel Mapping. Returns the docname, which changes when the
	label changes (the docname is derived from the label)."""
	_require_system_manager()
	name = _str(name, "name")
	label = _str(label, "label")
	type_ = _choice(type, "type", _VALID_CH_TYPES, _("Invalid channel type"))
	combinator_ = _combinator(combinator)
	rule_rows = _rules_list(rules)
	return _update_mapping(
		"Raven Channel Mapping", name, label, {"channel_type": type_}, rule_rows, combinator_
	)


@frappe.whitelist()
def delete_channel(name: str) -> None:
	"""Delete the Raven Channel Mapping.

	Deletes this app's own record only. The backing Raven Channel and its messages
	are left intact and simply become unmanaged.
	"""
	_require_system_manager()
	name = _str(name, "name")
	frappe.get_doc("Raven Channel Mapping", name).delete()


@frappe.whitelist()
def recreate_channel(name: str) -> str:
	"""Give a stale Raven Channel Mapping a fresh backing Raven Channel."""
	_require_system_manager()
	name = _str(name, "name")
	_require_stale("Raven Channel Mapping", name)

	doc = frappe.get_doc("Raven Channel Mapping", name)
	parent = frappe.db.get_value(
		"Raven Workspace Mapping", doc.workspace, ["workspace_label", "stale"], as_dict=True
	)
	if parent and parent.stale:
		frappe.throw(
			title=_("Recreate the workspace first"),
			msg=_(
				"<b>{0}</b> lives inside workspace <b>{1}</b>, whose Raven workspace was also "
				"deleted. Recreate <b>{1}</b>, then recreate this channel."
			).format(escape_html(doc.channel_label), escape_html(parent.workspace_label)),
		)

	from raven_integration.sync_service import create_raven_channel_for

	create_raven_channel_for(doc)
	frappe.db.set_value(
		"Raven Channel Mapping",
		name,
		{"raven_channel": doc.raven_channel, "stale": 0},
	)
	_enqueue_member_sync("Raven Channel Mapping", name)
	return doc.raven_channel


@frappe.whitelist()
def set_channel_enabled(name: str, enabled: bool) -> dict:
	"""Enable/disable membership sync for a single Raven Channel Mapping."""
	_require_system_manager()
	name = _str(name, "name")
	enabled = _bool(enabled, "enabled")
	return _set_mapping_enabled("Raven Channel Mapping", name, enabled)


@frappe.whitelist()
def set_channel_type(name: str, type: str) -> dict:
	"""Change a Raven Channel Mapping's visibility (Public/Private/Open)."""
	_require_system_manager()
	name = _str(name, "name")
	type_ = _choice(type, "type", _VALID_CH_TYPES, _("Invalid channel type"))
	return _set_mapping_type("Raven Channel Mapping", "channel_type", name, type_)


@frappe.whitelist()
def set_channel_label(name: str, label: str) -> dict:
	"""Rename a Raven Channel Mapping and its backing Raven Channel (inline edit)."""
	_require_system_manager()
	name = _str(name, "name")
	label = _str(label, "label")
	return _relabel_mapping(
		"Raven Channel Mapping", name, label, "ri_set_channel_label", _rename_raven_channel
	)


@frappe.whitelist()
def set_channel_combinator(name: str, combinator: str) -> dict:
	"""Change how a Raven Channel Mapping combines its rules (Any (OR) / All (AND))."""
	_require_system_manager()
	name = _str(name, "name")
	combinator_ = _combinator(combinator)
	return _set_mapping_combinator("Raven Channel Mapping", name, combinator_)


@frappe.whitelist()
def set_channel_rule_status(name: str, rule: str, status: str) -> dict:
	"""Pause/resume one member rule of a Raven Channel Mapping."""
	_require_system_manager()
	return _set_rule_status("Raven Channel Mapping", name, rule, status)


@frappe.whitelist()
def reconcile_now(target_doctype: str, name: str) -> dict:
	"""Queue a full member re-sync for one mapping."""
	_require_system_manager()
	target_doctype = _choice(
		target_doctype, "target_doctype", set(_MAPPING_LABEL_FIELDS), _("Unsupported sync target")
	)
	name = _str(name, "name")
	_require_mapping(target_doctype, name)
	_enqueue_member_sync(target_doctype, name)
	return {"queued": True}


@frappe.whitelist()
def preview_rule(rule: dict) -> dict:
	_require_system_manager()
	if not isinstance(rule, dict):
		frappe.throw(
			title=_("Invalid rule"),
			msg=_(
				"<b>Rule</b> must be a single rule, but the request sent {0}. "
				"Reload the page and try again."
			).format(type(rule).__name__),
		)
	from raven_integration.registry import evaluate as _evaluate
	from raven_integration.registry import validate_rule_config

	provider = rule.get("provider")
	rule_type = rule.get("rule_type")
	config = rule.get("config") or {}
	validate_rule_config(provider, rule_type, config)
	matched = _evaluate(provider, rule_type, config)
	return {
		"matched_user_count": len(matched),
		"sample_users": sorted(matched)[:5],
	}


@frappe.whitelist()
def compute_rule_diff(
	target_doctype: str,
	name: str,
	new_rules: "list[dict] | None" = None,
	combinator: "str | None" = None,
) -> dict:
	"""Return { added, removed, removed_users } for a proposed change."""
	_require_system_manager()
	target_doctype = _choice(
		target_doctype, "target_doctype", set(_MAPPING_LABEL_FIELDS), _("Unsupported sync target")
	)
	name = _str(name, "name")
	_require_mapping(target_doctype, name)
	if new_rules is None:
		new_rules = [
			_serialize_rule_for_ui(r)
			for r in frappe.get_doc(target_doctype, name).member_rules
		]
	# Previewing a diff saves nothing, so an unnamed rule is not an error here.
	new_rules = _rules_list(new_rules, require_label=False)

	from raven_integration.engine import (
		disabled_rule_members,
		evaluate_rules,
		has_active_rules,
	)

	# Mirror sync_service exactly, or the confirmation lies. It compares the rules
	# against who is *actually* rule-managed right now, not against the population
	# the old rules would produce, and it honours the same two guards: a rule set
	# with nothing active is skipped wholesale, and disabled rules freeze rather
	# than evict. Both of those mean "no removals", which a naive set difference
	# over the rules alone reports as removing everyone.
	if not has_active_rules(new_rules):
		return {"added": 0, "removed": 0, "removed_users": []}

	if target_doctype == "Raven Workspace Mapping":
		raven_link = frappe.db.get_value(target_doctype, name, "raven_workspace")
		current = set(
			frappe.get_all(
				"Raven Workspace Member",
				filters={"workspace": raven_link, "added_by_rule": 1},
				pluck="user",
			)
		) if raven_link else set()
	else:
		raven_link = frappe.db.get_value(target_doctype, name, "raven_channel")
		current = set(
			frappe.get_all(
				"Raven Channel Member",
				filters={"channel_id": raven_link, "added_by_rule": 1},
				pluck="user_id",
			)
		) if raven_link else set()

	combinator_ = (
		_combinator(combinator)
		if combinator is not None
		else (frappe.db.get_value(target_doctype, name, "rule_combinator") or "Any (OR)")
	)
	new_set = evaluate_rules(new_rules, strict=False, combinator=combinator_)
	frozen = current & disabled_rule_members(new_rules, strict=False)
	added = new_set - current
	removed = current - new_set - frozen

	return {
		"added": len(added),
		"removed": len(removed),
		"removed_users": sorted(removed)[:10],
	}


@frappe.whitelist()
def list_unmapped_workspaces() -> list[dict]:
	"""Raven Workspaces that no Raven Workspace Mapping manages yet.

	Uses frappe.qb with a NOT IN over the mappings' non-null raven_workspace links
	(specs/security.md §2 — no raw SQL)."""
	_require_system_manager()
	RW = frappe.qb.DocType("Raven Workspace")
	RWM = frappe.qb.DocType("Raven Workspace Mapping")
	mapped = (
		frappe.qb.from_(RWM).select(RWM.raven_workspace).where(RWM.raven_workspace.isnotnull())
	)
	return (
		frappe.qb.from_(RW)
		.select(RW.name, RW.workspace_name, RW.type)
		.where(RW.name.notin(mapped))
		.orderby(RW.workspace_name)
		.run(as_dict=True)
	)


@frappe.whitelist()
def link_workspace(
	raven_workspace: str,
	combinator: "str | None" = None,
	rules: "list[dict] | None" = None,
) -> str:
	"""Adopt an existing Raven Workspace into a new Raven Workspace Mapping.

	No new Raven Workspace is created: flags.skip_raven_create suppresses the
	before_insert provisioning and the mapping is pointed at the supplied id. The
	unique raven_workspace constraint (not an exists-then-insert) enforces one
	mapping per Raven workspace — a second attempt surfaces as 'already managed'.
	Returns the new mapping's docname."""
	_require_system_manager()
	raven_workspace = _str(raven_workspace, "raven_workspace")
	combinator_ = _combinator(combinator)
	rule_rows = _rules_list(rules) if rules else []

	ws = frappe.db.get_value(
		"Raven Workspace", raven_workspace, ["workspace_name", "type"], as_dict=True
	)
	if not ws:
		frappe.throw(
			title=_("Workspace not found"),
			msg=_(
				"No Raven Workspace named <b>{0}</b> exists. "
				"Reload the page and pick a workspace from the list."
			).format(escape_html(raven_workspace)),
			exc=frappe.DoesNotExistError,
		)

	doc = frappe.new_doc("Raven Workspace Mapping")
	doc.workspace_label = ws.workspace_name
	doc.workspace_type = ws.type if ws.type in _VALID_WS_TYPES else "Private"
	doc.rule_combinator = combinator_
	doc.raven_workspace = raven_workspace
	doc.flags.skip_raven_create = True
	for r in rule_rows:
		doc.append("member_rules", r)
	try:
		doc.insert()
	except frappe.DuplicateEntryError:
		frappe.throw(
			title=_("Workspace already managed"),
			msg=_(
				"<b>{0}</b> is already linked to a Raven Workspace Mapping. "
				"Reload the page — it should already appear in the list."
			).format(escape_html(raven_workspace)),
		)
	return doc.name


@frappe.whitelist()
def list_unmapped_channels(workspace: str) -> list[dict]:
	"""Raven Channels in a managed workspace that no Raven Channel Mapping manages yet.

	Direct-message and thread channels are excluded — only regular channels are
	adoptable. frappe.qb only."""
	_require_system_manager()
	workspace = _str(workspace, "workspace")
	_require_mapping("Raven Workspace Mapping", workspace)
	raven_workspace = frappe.db.get_value(
		"Raven Workspace Mapping", workspace, "raven_workspace"
	)
	if not raven_workspace:
		return []
	RC = frappe.qb.DocType("Raven Channel")
	RCM = frappe.qb.DocType("Raven Channel Mapping")
	mapped = frappe.qb.from_(RCM).select(RCM.raven_channel).where(RCM.raven_channel.isnotnull())
	return (
		frappe.qb.from_(RC)
		.select(RC.name, RC.channel_name, RC.type)
		.where(
			(RC.workspace == raven_workspace)
			& (RC.is_direct_message == 0)
			& (RC.is_thread == 0)
			& (RC.name.notin(mapped))
		)
		.orderby(RC.channel_name)
		.run(as_dict=True)
	)


@frappe.whitelist()
def link_channel(
	workspace: str,
	raven_channel: str,
	combinator: "str | None" = None,
	rules: "list[dict] | None" = None,
) -> str:
	"""Adopt an existing Raven Channel into a new Raven Channel Mapping under ``workspace``.

	``workspace`` is the parent Raven Workspace Mapping name. No new Raven Channel is
	created: flags.skip_raven_create suppresses provisioning and the mapping is
	pointed at the supplied id. The unique raven_channel constraint enforces one
	mapping per Raven channel — a second attempt surfaces as 'already managed'.
	Returns the new mapping's docname."""
	_require_system_manager()
	workspace = _str(workspace, "workspace")
	raven_channel = _str(raven_channel, "raven_channel")
	combinator_ = _combinator(combinator)
	rule_rows = _rules_list(rules) if rules else []

	_require_mapping("Raven Workspace Mapping", workspace)
	ch = frappe.db.get_value(
		"Raven Channel", raven_channel, ["channel_name", "type"], as_dict=True
	)
	if not ch:
		frappe.throw(
			title=_("Channel not found"),
			msg=_(
				"No Raven Channel named <b>{0}</b> exists. "
				"Reload the page and pick a channel from the list."
			).format(escape_html(raven_channel)),
			exc=frappe.DoesNotExistError,
		)

	doc = frappe.new_doc("Raven Channel Mapping")
	doc.channel_label = ch.channel_name
	doc.workspace = workspace
	doc.channel_type = ch.type if ch.type in _VALID_CH_TYPES else "Private"
	doc.rule_combinator = combinator_
	doc.raven_channel = raven_channel
	doc.flags.skip_raven_create = True
	for r in rule_rows:
		doc.append("member_rules", r)
	try:
		doc.insert()
	except frappe.DuplicateEntryError:
		frappe.throw(
			title=_("Channel already managed"),
			msg=_(
				"<b>{0}</b> is already linked to a Raven Channel Mapping. "
				"Reload the page — it should already appear in the list."
			).format(escape_html(raven_channel)),
		)
	return doc.name
