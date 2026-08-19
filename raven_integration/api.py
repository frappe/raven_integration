from typing import Any

import frappe
from frappe import _
from frappe.query_builder.functions import Count
from frappe.utils import escape_html

from raven_integration.engine import CONJUNCTION_OR, MAX_TREE_DEPTH, MAX_TREE_NODES, is_group
from raven_integration.utils import raven_installed

_VALID_WS_TYPES = {"Public", "Private"}
_VALID_CH_TYPES = {"Public", "Private", "Open"}
# What a group may join its conditions with. The UI writes one of them per group.
_VALID_CONJUNCTIONS = {"and", "or"}
_VALID_RULE_STATUSES = {"Active", "Paused"}

# Mapping doctype -> its free-text label field. Both autoname as
# `format:<PREFIX>-{label}`, so the docname is derived from the label.
_MAPPING_LABEL_FIELDS = {
	"Raven Workspace Mapping": "workspace_label",
	"Raven Channel Mapping": "channel_label",
}


def _require_manager() -> None:
	"""Gate every management endpoint. System Manager, plus any role a host app
	declares — see raven_integration.permissions.manager_roles."""
	from raven_integration.permissions import require_manager

	require_manager()


def _str(value: Any, field: str) -> str:
	if not isinstance(value, str):
		frappe.throw(
			title=_("Invalid value for {0}").format(field),
			msg=_("<b>{0}</b> must be text, but the request sent {1}. Reload the page and try again.").format(
				field, type(value).__name__
			),
		)
	return value


def _bool(value: Any, field: str) -> bool:
	if not isinstance(value, bool):
		frappe.throw(
			title=_("Invalid value for {0}").format(field),
			msg=_(
				"<b>{0}</b> must be true or false, but the request sent {1}. Reload the page and try again."
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


def _row_label(path: "list[int]") -> str:
	"""A path rendered for an error message: [0, 2] → "1.3", counting from one."""
	return ".".join(str(i + 1) for i in path)


def _rule_leaf(value: dict, path: "list[int]", require_label: bool) -> dict:
	"""Validate one leaf of a condition tree, returning the cleaned rule.

	``require_label`` is off for the read-only preview path: a name identifies a rule
	to the user and is required to *save* one, but it has no bearing on who a rule
	matches, and a stored rule may still be unnamed (labels the old backend
	generated are blanked by the migration patch).

	The fields are typed here rather than left to the doctype, which no longer sees
	them: the tree is written to a JSON column, so nothing downstream would reject a
	rule_type that arrived as a list.
	"""
	rule = {
		"label": value.get("label"),
		"provider": _str(value.get("provider"), f"provider (condition {_row_label(path)})"),
		"rule_type": _str(value.get("rule_type"), f"rule_type (condition {_row_label(path)})"),
		"status": _choice(
			value.get("status") or "Active",
			f"status (condition {_row_label(path)})",
			_VALID_RULE_STATUSES,
			_("Invalid rule status"),
		),
		"config": value.get("config") or {},
	}
	if not isinstance(rule["config"], dict):
		frappe.throw(
			title=_("Invalid rule settings"),
			msg=_(
				"The settings of condition <b>{0}</b> must be an object, but the request sent {1}. "
				"Reload the page and try again."
			).format(_row_label(path), type(rule["config"]).__name__),
		)
	label = rule["label"]
	if require_label:
		# A rule name is the only thing that tells two rules of the same type apart,
		# so it is required rather than defaulted. Rejecting it here — before any
		# field on the mapping is touched — gives the user a message naming the row,
		# instead of the MandatoryError doc.save() would raise further down.
		if not isinstance(label, str) or not label.strip():
			frappe.throw(
				title=_("Rule name is required"),
				msg=_(
					"Condition <b>{0}</b> has no name. A name is what tells two rules of the "
					"same type apart. Name the condition, then save again."
				).format(_row_label(path)),
			)
	rule["label"] = label.strip() if isinstance(label, str) else None
	return rule


def _rule_node(value: Any, path: "list[int]", require_label: bool, depth: int, budget: list) -> dict:
	"""Validate one node — group or leaf — of a condition tree, recursively."""
	if not isinstance(value, dict):
		frappe.throw(
			title=_("Invalid member rule"),
			msg=_(
				"Condition <b>{0}</b> must be a rule or a group, but the request sent {1}. "
				"Reload the page and try again."
			).format(_row_label(path), type(value).__name__),
		)
	budget[0] -= 1
	if budget[0] < 0:
		frappe.throw(
			title=_("Too many conditions"),
			msg=_("A channel can hold at most {0} conditions. Remove some, then save again.").format(
				MAX_TREE_NODES
			),
		)
	if not is_group(value):
		return _rule_leaf(value, path, require_label)

	if depth >= MAX_TREE_DEPTH:
		frappe.throw(
			title=_("Conditions nested too deeply"),
			msg=_(
				"Condition groups can be nested {0} levels deep. Flatten group <b>{1}</b>, then save again."
			).format(MAX_TREE_DEPTH, _row_label(path)),
		)
	conditions = value.get("conditions")
	if not isinstance(conditions, list):
		frappe.throw(
			title=_("Invalid condition group"),
			msg=_(
				"Group <b>{0}</b> must hold a list of conditions, but the request sent {1}. "
				"Reload the page and try again."
			).format(_row_label(path) or _("the outermost group"), type(conditions).__name__),
		)
	conjunctions = value.get("conjunctions")
	if not isinstance(conjunctions, list) or any(c not in _VALID_CONJUNCTIONS for c in conjunctions):
		frappe.throw(
			title=_("Invalid condition group"),
			msg=_(
				"Group <b>{0}</b> must join its conditions with {1}. Reload the page and try again."
			).format(_row_label(path) or _("the outermost group"), " / ".join(sorted(_VALID_CONJUNCTIONS))),
		)
	# One joiner per gap. A mismatch is a payload this app did not write, and
	# guessing which conditions it meant to join would silently rewrite the rule.
	if len(conjunctions) != max(len(conditions) - 1, 0):
		frappe.throw(
			title=_("Invalid condition group"),
			msg=_(
				"Group <b>{0}</b> holds {1} conditions but {2} joiners between them. "
				"Reload the page and try again."
			).format(_row_label(path) or _("the outermost group"), len(conditions), len(conjunctions)),
		)
	return {
		"conjunctions": list(conjunctions),
		"conditions": [
			_rule_node(child, [*path, i], require_label, depth + 1, budget)
			for i, child in enumerate(conditions)
		],
	}


def _rule_tree(value: Any, require_label: bool = True) -> dict:
	"""Validate a whole condition-tree payload, returning the cleaned tree.

	``None`` reads as "no conditions" — the shape a channel that has never been given
	a rule is created with. An endpoint where a missing tree means "do not touch the
	stored one" (update_channel) decides that before calling: the two are different
	answers and only the caller knows which it meant.
	"""
	if value is None:
		return {"conjunctions": [], "conditions": []}
	tree = _rule_node(value, [], require_label, 0, [MAX_TREE_NODES])
	if not is_group(tree):
		frappe.throw(
			title=_("Invalid member rules"),
			msg=_("<b>Member Rules</b> must be a group of conditions. Reload the page and try again."),
		)
	return tree


def _require_mapping(doctype: str, name: str) -> None:
	"""Fail loudly on an unknown mapping name.

	`db.set_value` on a row that does not exist is a silent no-op, so without this
	the UI reports success for a change that never landed."""
	if not frappe.db.exists(doctype, name):
		frappe.throw(
			title=_("{0} not found").format(_(doctype)),
			msg=_(
				"No {0} named <b>{1}</b> exists. It may have been deleted — reload the page and try again."
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


def _rule_path(value: Any) -> "list[int]":
	"""Validate a leaf address: the child indices from the root of the tree."""
	if isinstance(value, str):
		# Over HTTP a list arrives as JSON text. Anything that is not JSON is a bad
		# request, not a server error, so it takes the same message as a bad shape.
		try:
			value = frappe.parse_json(value)
		except (ValueError, TypeError):
			value = None
	# `True` is an int in Python, and indexing a list with it silently means 1.
	if (
		not isinstance(value, list)
		or not value
		or any(isinstance(i, bool) or not isinstance(i, int) or i < 0 for i in value)
	):
		frappe.throw(
			title=_("Invalid condition"),
			msg=_("A condition is addressed by its position in the tree. Reload the page and try again."),
		)
	return list(value)


def _leaf_at(tree: dict, path: "list[int]") -> dict:
	"""The leaf at ``path``, or a loud failure if nothing is there.

	A rule has no docname to check against a parent any more; its address is its
	position, so "does it exist" and "does it belong to this mapping" are the same
	question, answered against the mapping's own tree.
	"""
	node: Any = tree
	for index in path:
		conditions = node.get("conditions") if is_group(node) else None
		if not conditions or index >= len(conditions):
			node = None
			break
		node = conditions[index]
	if not isinstance(node, dict) or is_group(node):
		frappe.throw(
			title=_("Condition not found"),
			msg=_(
				"No condition at position <b>{0}</b> on this channel. It may have been moved "
				"or removed — reload the page and try again."
			).format(_row_label(path)),
			exc=frappe.DoesNotExistError,
		)
	return node


def _set_rule_status(doctype: str, name: str, path: Any, status: str) -> dict:
	"""Shared body of set_channel_rule_status."""
	from raven_integration.engine import parse_tree, pausable

	name = _str(name, "name")
	path_ = _rule_path(path)
	status_ = _choice(status, "status", _VALID_RULE_STATUSES, _("Invalid rule status"))
	_require_mapping(doctype, name)

	# Written through the JSON field rather than through doc.save() — deliberately.
	# A full save revalidates every rule on the mapping, and this endpoint exists
	# precisely to reach a rule a full save cannot: a Paused, unnamed one, whose card
	# the UI freezes and whose save path the name check rejects. _schedule_resync()
	# supplies the membership refresh a skipped save would have triggered.
	#
	# The read locks the row (for_update, on the primary key), because the write puts
	# the whole tree back: two managers pausing two different conditions would each
	# otherwise write a tree that predates the other's edit, and one of the two pauses
	# would silently vanish. A plain read cannot see the other session's commit —
	# under REPEATABLE READ it is answered from the snapshot this transaction opened
	# with — so the lock, which reads current, is what makes the second write correct.
	stored = frappe.db.get_value(doctype, name, "member_rules_json", for_update=True)
	tree = parse_tree(stored) or {"conjunctions": [], "conditions": []}
	leaf = _leaf_at(tree, path_)
	if status_ == "Paused" and not pausable(tree, path_):
		frappe.throw(
			title=_("This condition cannot be paused"),
			msg=_(
				"Pausing a condition freezes who it already added instead of dropping it, which "
				"only holds while the condition <i>adds</i> people. Condition <b>{0}</b> sits in a "
				"group joined by <b>and</b>, where it narrows the group instead — pausing it there "
				"would add people, not hold them. Remove it, or join its group with <b>or</b>."
			).format(_row_label(path_)),
		)
	leaf["status"] = status_
	frappe.db.set_value(doctype, name, "member_rules_json", frappe.as_json(tree))
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
					"Another {0} is already called <b>{1}</b>. Pick a different name and try again."
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
				"A Raven workspace named <b>{0}</b> already exists. Pick a different name and try again."
			).format(escape_html(label)),
		)
	# Suppress the reverse after_rename handler: this rename originates from the
	# mapping, and _relabel_mapping renames the mapping itself right after. Without
	# this, the reverse handler would rename the mapping first and the caller's own
	# rename would then fail on a docname that no longer exists.
	# ignore_permissions like every other Raven-side write in this app: the endpoint
	# has already authorized the caller, and the app only ever touches the Raven
	# records it created itself. Without it a manager who is not a Raven admin has
	# no write permission on Raven Workspace and the rename fails half way through,
	# after the mapping has been relabelled. frappe.rename_doc does not forward
	# ignore_permissions, so this goes through the model function directly.
	from frappe.model.rename_doc import rename_doc

	from raven_integration.sync_service import pushing_to_raven

	with pushing_to_raven():
		rename_doc(
			doctype="Raven Workspace",
			old=row.raven_workspace,
			new=label,
			ignore_permissions=True,
			show_alert=False,
		)


def _rename_raven_channel(mapping_name: str, label: str) -> None:
	"""Propagate a channel-mapping label change to its backing Raven Channel.

	A Raven Channel's name/id is independent of its display, so only the
	channel_name field changes; the id and this mapping's raven_channel Link stay
	valid. Raven slugifies channel_name and enforces per-workspace name uniqueness,
	so a name Raven rejects surfaces as a friendly error. Skipped when the mapping
	is stale or unlinked."""
	row = frappe.db.get_value("Raven Channel Mapping", mapping_name, ["raven_channel", "stale"], as_dict=True)
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
	alloc_title: str,
	alloc_msg: str,
	rule_tree: "dict | None" = None,
) -> str:
	"""Shared body of create_workspace / create_channel. ``fields`` carries the
	doctype-specific columns (type, and a channel's parent workspace); with no label,
	retries the next free default so a session losing the race still gets a row.

	``rule_tree`` is channel-only — a workspace holds no rules."""
	explicit = bool(label and str(label).strip())
	if explicit:
		label = _str(label, "label")

	tried: set[str] = set()
	for _attempt in range(5):
		chosen = label if explicit else _next_default_label(doctype, base, skip=tried)
		doc = frappe.new_doc(doctype)
		doc.set(_MAPPING_LABEL_FIELDS[doctype], chosen)
		doc.update(fields)
		if rule_tree is not None:
			doc.member_rules_json = frappe.as_json(rule_tree)
		try:
			doc.insert()
			return doc.name
		except frappe.DuplicateEntryError:
			if explicit:
				raise  # caller-chosen name already exists
			tried.add(chosen)
	frappe.throw(title=alloc_title, msg=alloc_msg)


def _update_mapping(
	doctype: str,
	name: str,
	label: str,
	fields: dict,
	rule_tree: "dict | None" = None,
	rename_raven=None,
	savepoint: str = "update_mapping",
) -> str:
	"""Shared body of update_workspace / update_channel. Returns the (possibly new)
	docname, which changes when the label changes.

	``rule_tree`` is channel-only, and ``None`` means "leave the stored conditions
	alone" — an empty group is a tree like any other, and does clear them. A workspace
	update passes no tree at all, so it also skips the resync: nothing it can change
	moves a member.

	``rename_raven`` carries a changed label to the backing Raven record, the same
	propagation `_relabel_mapping` does for the single-field endpoints. Without it
	a label written through this path renames only the mapping, and the Raven side
	keeps the old name forever — the two silently diverge. It shares one savepoint
	with the mapping write so neither side is ever left half-renamed."""
	doc = frappe.get_doc(doctype, name)
	stored_label = doc.get(_MAPPING_LABEL_FIELDS[doctype])
	# Only an actual change is propagated: re-saving an unchanged name would touch
	# the Raven record on every save, and Raven validates uniqueness on write.
	propagate = rename_raven is not None and label != stored_label

	if propagate:
		frappe.db.savepoint(savepoint)
	try:
		doc.update(fields)
		if rule_tree is not None:
			doc.member_rules_json = frappe.as_json(rule_tree)
		doc.save()
		if propagate:
			rename_raven(name, label)
		new_name = _set_mapping_label(doctype, name, label)
	except Exception:
		if propagate:
			frappe.db.rollback(save_point=savepoint)
		raise

	# After the write lands, never before: a rolled-back save must not leave a
	# resync queued against rules that were not stored.
	if rule_tree is not None:
		_schedule_resync()
	return new_name


def _set_mapping_type(doctype: str, field: str, name: str, type_: str) -> dict:
	"""Shared body of set_workspace_type / set_channel_type. Saves through the doc
	(not db.set_value) so the controller's on_update carries the new visibility onto
	the backing Raven workspace/channel."""
	_require_mapping(doctype, name)
	doc = frappe.get_doc(doctype, name)
	doc.set(field, type_)
	doc.save()
	return {"type": type_}


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
		decl = {rt["type"]: rt for rt in list_rule_types(rule.get("provider"))}.get(rule.get("rule_type"))
	except Exception:
		decl = None
	return (decl.get("label") if decl else None) or rule.get("rule_type") or ""


def _serialize_rule_for_ui(rule: dict) -> dict:
	"""One leaf, as the frontend wants it. config is opaque to the core."""
	config = frappe.parse_json(rule.get("config")) if rule.get("config") else {}
	return {
		"label": rule.get("label"),
		"provider": rule.get("provider"),
		"rule_type": rule.get("rule_type"),
		"status": rule.get("status") or "Active",
		"config": config if isinstance(config, dict) else {},
		"matches": _describe_rule(rule),
	}


def _serialize_tree_for_ui(tree) -> dict:
	"""The stored tree with every leaf annotated for the rules panel.

	Groups are passed through with their own shape intact, so what the panel edits
	and what it sends back are the same object — the component and this app agree on
	the model, which is the point of storing the component's own tree.
	"""
	from raven_integration.engine import is_group as _is_group_node
	from raven_integration.engine import parse_tree

	def walk(node: dict) -> dict:
		if not _is_group_node(node):
			return _serialize_rule_for_ui(node)
		stored = node.get("conditions") or []
		joiners = list(node.get("conjunctions") or [])
		conditions: list[dict] = []
		conjunctions: list[str] = []
		for index, child in enumerate(stored):
			# A child that is not a rule or a group cannot be drawn, and dropping it
			# has to drop the gap it stood in too — the panel sends back what it was
			# served, and the save path rejects a group with a joiner per condition
			# instead of a joiner per gap.
			if not isinstance(child, dict):
				continue
			if conditions:
				# A gap the stored tree does not name reads as "or" here because that
				# is how the engine folds it — the panel is shown what is evaluated.
				joiner = joiners[index - 1] if index - 1 < len(joiners) else CONJUNCTION_OR
				conjunctions.append(joiner)
			conditions.append(walk(child))
		return {"conjunctions": conjunctions, "conditions": conditions}

	return walk(parse_tree(tree) or {"conjunctions": [], "conditions": []})


def _schedule_resync() -> None:
	"""Queue a membership resync after a change to a mapping's own conditions.

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

	Managers only: the reply enumerates installed apps, and every action the Settings
	panel offers already requires the same roles."""
	_require_manager()
	# Active, not installed: an app that has been disabled on this site is still in
	# installed_apps, but its hooks do not load and its scheduled jobs do not run,
	# so sync cannot work. Reporting it as present opens the Settings gate on an
	# integration that will silently do nothing.
	apps = frappe.get_active_apps()
	return {
		"raven": "raven" in apps,
		"raven_integration": "raven_integration" in apps,
		"enabled": bool(frappe.db.get_single_value("Raven Membership Settings", "enabled")),
	}


@frappe.whitelist()
def enable_integration() -> dict:
	"""Enable membership sync (one-way — there is no disable). Triggers an initial reconcile."""
	_require_manager()
	frappe.db.set_single_value("Raven Membership Settings", "enabled", 1)
	frappe.enqueue("raven_integration.scheduler.reconcile_all", queue="long", enqueue_after_commit=True)
	return {"enabled": True}


@frappe.whitelist()
def list_providers() -> list:
	"""Membership providers registered by consumer apps (UI rule-type discovery)."""
	_require_manager()
	from raven_integration.registry import list_providers as _list

	return _list()


def _channel_counts(workspaces: list[str]) -> dict[str, int]:
	"""How many channel mappings sit under each workspace mapping, in one query."""
	if not workspaces:
		return {}
	RCM = frappe.qb.DocType("Raven Channel Mapping")
	rows = (
		frappe.qb.from_(RCM)
		.select(RCM.workspace, Count(RCM.name).as_("count"))
		.where(RCM.workspace.isin(workspaces))
		.groupby(RCM.workspace)
		.run(as_dict=True)
	)
	return {row.workspace: row.count for row in rows}


@frappe.whitelist()
def list_workspaces() -> list[dict]:
	"""Every Raven workspace the UI can show: managed mappings newest first, then
	unmanaged Raven workspaces re-projected into the same row shape."""
	_require_manager()
	managed = frappe.get_all(
		"Raven Workspace Mapping",
		fields=[
			"name",
			"workspace_label",
			"workspace_type",
			"raven_workspace",
			"stale",
		],
		# By creation, never `modified`: the UI reloads this list after every edit,
		# so ordering by `modified` would send the edited row to the top and shift
		# every row under the pointer — an edit made from the row menu would land
		# somewhere else by the time the list came back. Creation still puts a
		# freshly created row first, which is where the create flow expects to find
		# and select it.
		order_by="creation desc",
	)
	counts = _channel_counts([row["name"] for row in managed])
	for row in managed:
		row["mapped"] = True
		row["channel_count"] = counts.get(row["name"], 0)
	unmanaged = [
		{
			"mapped": False,
			"name": None,
			"workspace_label": w["workspace_name"],
			"workspace_type": w["type"],
			"raven_workspace": w["name"],
			"stale": 0,
			# Nothing is managed under an unadopted workspace, so a count of 0 would
			# read as "this workspace has no channels". It has none *here*.
			"channel_count": None,
		}
		for w in list_unmapped_workspaces()
	]
	return managed + unmanaged


@frappe.whitelist()
def get_workspace(name: str) -> dict:
	"""Full workspace detail for the detail page. Carries no rules: a workspace's
	membership is derived from whoever is in at least one of its channels."""
	_require_manager()
	name = _str(name, "name")
	from raven_integration.engine import expected_workspace_members

	doc = frappe.get_doc("Raven Workspace Mapping", name)
	channels = frappe.get_all("Raven Channel Mapping", filters={"workspace": name}, fields=["enabled"])
	return {
		"name": doc.name,
		"workspace_label": doc.workspace_label,
		"workspace_type": doc.workspace_type,
		"stale": doc.stale,
		"raven_workspace": doc.raven_workspace,
		"creation": doc.creation,
		"member_count": len(expected_workspace_members(name)),
		"channels_active": sum(1 for c in channels if c.enabled),
		"channels_paused": sum(1 for c in channels if not c.enabled),
	}


@frappe.whitelist()
def list_workspace_members(name: str) -> list[dict]:
	"""The workspace's derived membership, each entry naming the channels it came from.

	Read-only by construction: nothing here is settable, because membership is not
	stored on the workspace — it is a consequence of the channel memberships below
	each row. A user in no channel does not appear, even if a stale Raven Workspace
	Member row still exists for them.
	"""
	_require_manager()
	name = _str(name, "name")
	_require_mapping("Raven Workspace Mapping", name)
	raven_workspace = frappe.db.get_value("Raven Workspace Mapping", name, "raven_workspace")
	if not raven_workspace or not raven_installed():
		return []

	RC = frappe.qb.DocType("Raven Channel")
	RCM = frappe.qb.DocType("Raven Channel Member")
	rows = (
		frappe.qb.from_(RCM)
		.join(RC)
		.on(RC.name == RCM.channel_id)
		.select(RCM.user_id, RCM.added_by_rule, RC.channel_name)
		.where(RC.workspace == raven_workspace)
		.orderby(RCM.user_id)
		.orderby(RC.channel_name)
		.run(as_dict=True)
	)

	members: dict[str, dict] = {}
	for row in rows:
		member = members.setdefault(
			row.user_id,
			{
				"user": row.user_id,
				"full_name": row.user_id,
				"user_image": None,
				"channels": [],
				"added_by_rule": False,
			},
		)
		if row.channel_name not in member["channels"]:
			member["channels"].append(row.channel_name)
		if row.added_by_rule:
			member["added_by_rule"] = True

	names = frappe.get_all(
		"User", filters={"name": ("in", list(members))}, fields=["name", "full_name", "user_image"]
	)
	for user in names:
		if user.full_name:
			members[user.name]["full_name"] = user.full_name
		members[user.name]["user_image"] = user.user_image
	return sorted(members.values(), key=lambda m: m["full_name"].lower())


@frappe.whitelist()
def create_workspace(label: "str | None" = None, type: str = "Private") -> str:
	"""Create a Raven Workspace Mapping. With no label, auto-names 'Workspace N'
	(next free number) so the UI can add a row in one click."""
	_require_manager()
	type_ = _choice(type, "type", _VALID_WS_TYPES, _("Invalid workspace type"))
	return _create_mapping(
		"Raven Workspace Mapping",
		"Workspace",
		label,
		{"workspace_type": type_},
		_("Could not allocate a default workspace name"),
		_(
			"Another session claimed the auto-generated name on every attempt. "
			"Try again, or create the workspace with a name of your own."
		),
	)


@frappe.whitelist()
def update_workspace(name: str, label: str, type: str) -> str:
	"""Update a Raven Workspace Mapping. Returns the docname, which changes when
	the label changes (the docname is derived from the label)."""
	_require_manager()
	name = _str(name, "name")
	label = _str(label, "label")
	type_ = _choice(type, "type", _VALID_WS_TYPES, _("Invalid workspace type"))
	return _update_mapping(
		"Raven Workspace Mapping",
		name,
		label,
		{"workspace_type": type_},
		# The settings page commits name and visibility through this one call, so it
		# is a path a rename travels — and a Raven Workspace's docname *is* its
		# display name, with nothing syncing it back. Without this the two names part
		# company permanently, exactly as they did on the channel side.
		rename_raven=_rename_raven_workspace,
		savepoint="update_workspace",
	)


@frappe.whitelist()
def delete_workspace(name: str) -> None:
	"""Delete the Raven Workspace Mapping, its child channel mappings, and the
	membership their rules had granted.

	Of Raven's own records it deletes none: the backing Raven Workspace, its
	channels and their history are left intact and simply become unmanaged, as are
	the members a human added there. Deleting the workspace itself is Raven's
	decision to make, not ours. Raven does post one "X was removed by Y." system
	message per withdrawn member on the way out — it honours no suppression flag, so
	the withdrawal is visible in the channels it touches.
	"""
	_require_manager()
	name = _str(name, "name")
	frappe.get_doc("Raven Workspace Mapping", name).delete()


@frappe.whitelist()
def recreate_workspace(name: str) -> str:
	"""Give a stale Raven Workspace Mapping a fresh backing Raven Workspace."""
	_require_manager()
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
def set_workspace_type(name: str, type: str) -> dict:
	"""Change a Raven Workspace Mapping's visibility (Public/Private)."""
	_require_manager()
	name = _str(name, "name")
	type_ = _choice(type, "type", _VALID_WS_TYPES, _("Invalid workspace type"))
	return _set_mapping_type("Raven Workspace Mapping", "workspace_type", name, type_)


@frappe.whitelist()
def set_workspace_label(name: str, label: str) -> dict:
	"""Rename a Raven Workspace Mapping and its backing Raven Workspace (inline edit)."""
	_require_manager()
	name = _str(name, "name")
	label = _str(label, "label")
	return _relabel_mapping(
		"Raven Workspace Mapping", name, label, "ri_set_workspace_label", _rename_raven_workspace
	)


@frappe.whitelist()
def list_channels(workspace: str) -> list[dict]:
	"""Every channel in ``workspace`` the UI can show: managed channel mappings newest
	first, then unmanaged Raven channels in the backing workspace."""
	_require_manager()
	workspace = _str(workspace, "workspace")
	managed = frappe.get_all(
		"Raven Channel Mapping",
		filters={"workspace": workspace},
		fields=[
			"name",
			"channel_label",
			"channel_type",
			"raven_channel",
			"enabled",
			"stale",
		],
		# Stable across edits, for the reason spelled out in list_workspaces.
		order_by="creation desc",
	)
	for row in managed:
		row["mapped"] = True
	# No backing Raven workspace to scan (unknown/unlinked/stale parent) → managed only.
	unmapped_rows = (
		list_unmapped_channels(workspace) if frappe.db.exists("Raven Workspace Mapping", workspace) else []
	)
	unmanaged = [
		{
			"mapped": False,
			"name": None,
			"channel_label": c["channel_name"],
			"channel_type": c["type"],
			"raven_channel": c["name"],
			"enabled": 1,
			"stale": 0,
		}
		for c in unmapped_rows
	]
	return managed + unmanaged


@frappe.whitelist()
def get_channel(name: str) -> dict:
	"""Full channel detail, incl. the condition tree the rules panel edits."""
	_require_manager()
	name = _str(name, "name")
	from raven_integration.engine import evaluate_rules_or_unknown

	doc = frappe.get_doc("Raven Channel Mapping", name)
	# Not expected_channel_members: it folds "no provider could answer" into the
	# empty set, and len(set()) reads on screen as "this channel matches nobody"
	# — the one thing an unevaluable tree does not say. member_count stays an int
	# (the rules panel renders it straight into "{0} members" with no null guard),
	# and the flag beside it carries the distinction.
	members = evaluate_rules_or_unknown(doc.member_rules_json, strict=False)
	return {
		"name": doc.name,
		"channel_label": doc.channel_label,
		"workspace": doc.workspace,
		"channel_type": doc.channel_type,
		"enabled": doc.enabled,
		"stale": doc.stale,
		"raven_channel": doc.raven_channel,
		"member_count": len(members) if members is not None else 0,
		"member_count_unknown": members is None,
		"rules": _serialize_tree_for_ui(doc.member_rules_json),
	}


@frappe.whitelist()
def create_channel(
	workspace: str,
	label: "str | None" = None,
	type: str = "Private",
	rules: "dict | None" = None,
) -> str:
	"""Create a Raven Channel Mapping. With no label, auto-names 'Channel N'
	(next free number) so the UI can add a row in one click."""
	_require_manager()
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
	rule_tree = _rule_tree(rules)
	return _create_mapping(
		"Raven Channel Mapping",
		"Channel",
		label,
		{"workspace": workspace, "channel_type": type_},
		_("Could not allocate a default channel name"),
		_(
			"Another session claimed the auto-generated name on every attempt. "
			"Try again, or create the channel with a name of your own."
		),
		rule_tree=rule_tree,
	)


@frappe.whitelist()
def update_channel(
	name: str,
	label: str,
	type: str,
	rules: "dict | None" = None,
) -> str:
	"""Update a Raven Channel Mapping. Returns the docname, which changes when the
	label changes (the docname is derived from the label).

	Omitting ``rules`` leaves the channel's conditions exactly as they are; an empty
	group is how a caller says "remove every condition". The two have to be different
	answers: a caller renaming a channel sends no tree, and reading that as the empty
	tree deletes every condition it has and evicts everyone those conditions added."""
	_require_manager()
	name = _str(name, "name")
	label = _str(label, "label")
	type_ = _choice(type, "type", _VALID_CH_TYPES, _("Invalid channel type"))
	rule_tree = _rule_tree(rules) if rules is not None else None
	return _update_mapping(
		"Raven Channel Mapping",
		name,
		label,
		{"channel_type": type_},
		rule_tree=rule_tree,
		# The settings page commits name, visibility and conditions through this one
		# call, so it is now the path a rename travels — it has to reach Raven the
		# same way set_channel_label does.
		rename_raven=_rename_raven_channel,
		savepoint="update_channel",
	)


@frappe.whitelist()
def delete_channel(name: str) -> None:
	"""Delete the Raven Channel Mapping and the membership its rules had granted.

	The backing Raven Channel and its history are left intact and simply become
	unmanaged, as are the members a human added there. Raven does post one "X was
	removed by Y." system message per withdrawn member on the way out — it honours no
	suppression flag, so the withdrawal is visible in the channel.
	"""
	_require_manager()
	name = _str(name, "name")
	frappe.get_doc("Raven Channel Mapping", name).delete()


@frappe.whitelist()
def recreate_channel(name: str) -> str:
	"""Give a stale Raven Channel Mapping a fresh backing Raven Channel."""
	_require_manager()
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
	"""Enable/disable membership sync for a single Raven Channel Mapping.

	A channel is the only thing that carries this. Its parent workspace has no
	on/off of its own: workspace membership is derived from whoever is in the
	channels, so switching the channels off is what stops a workspace syncing.
	"""
	_require_manager()
	name = _str(name, "name")
	enabled = _bool(enabled, "enabled")
	_require_mapping("Raven Channel Mapping", name)
	frappe.db.set_value("Raven Channel Mapping", name, "enabled", 1 if enabled else 0)
	if enabled:
		_enqueue_member_sync("Raven Channel Mapping", name)
	return {"enabled": enabled}


@frappe.whitelist()
def set_channel_type(name: str, type: str) -> dict:
	"""Change a Raven Channel Mapping's visibility (Public/Private/Open)."""
	_require_manager()
	name = _str(name, "name")
	type_ = _choice(type, "type", _VALID_CH_TYPES, _("Invalid channel type"))
	return _set_mapping_type("Raven Channel Mapping", "channel_type", name, type_)


@frappe.whitelist()
def set_channel_label(name: str, label: str) -> dict:
	"""Rename a Raven Channel Mapping and its backing Raven Channel (inline edit)."""
	_require_manager()
	name = _str(name, "name")
	label = _str(label, "label")
	return _relabel_mapping(
		"Raven Channel Mapping", name, label, "ri_set_channel_label", _rename_raven_channel
	)


@frappe.whitelist()
def set_channel_rule_status(name: str, path: Any, status: str) -> dict:
	"""Pause/resume one condition of a Raven Channel Mapping, addressed by its path."""
	_require_manager()
	return _set_rule_status("Raven Channel Mapping", name, path, status)


@frappe.whitelist()
def reconcile_now(target_doctype: str, name: str) -> dict:
	"""Queue a full member re-sync for one mapping."""
	_require_manager()
	target_doctype = _choice(
		target_doctype, "target_doctype", set(_MAPPING_LABEL_FIELDS), _("Unsupported sync target")
	)
	name = _str(name, "name")
	_require_mapping(target_doctype, name)
	_enqueue_member_sync(target_doctype, name)
	return {"queued": True}


@frappe.whitelist()
def preview_rule(rule: dict) -> dict:
	"""How many users one rule matches, with a few sample names.

	The count is now exactly what the rule contributes to its channel: channels no
	longer intersect with a workspace population, so nothing narrows this afterwards.
	Only an ``and`` in the rule's own group can, by intersecting it with a sibling.
	"""
	_require_manager()
	if not isinstance(rule, dict):
		frappe.throw(
			title=_("Invalid rule"),
			msg=_(
				"<b>Rule</b> must be a single rule, but the request sent {0}. Reload the page and try again."
			).format(type(rule).__name__),
		)
	from raven_integration.registry import evaluate as _evaluate
	from raven_integration.registry import validate_rule_config

	# Typed here for the same reason _rule_leaf types the save path: nothing
	# downstream rejects a provider that arrived as a list. Unvalidated, the three
	# fields reach a dict lookup, a bare json.loads and an attribute access, and the
	# user gets a traceback instead of a message naming the field.
	provider = _str(rule.get("provider"), "provider")
	rule_type = _str(rule.get("rule_type"), "rule_type")
	config = rule.get("config") or {}
	if not isinstance(config, dict):
		frappe.throw(
			title=_("Invalid rule settings"),
			msg=_(
				"The settings of this condition must be an object, but the request sent {0}. "
				"Reload the page and try again."
			).format(type(config).__name__),
		)
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
	new_rules: "dict | None" = None,
) -> dict:
	"""Return { added, removed, removed_users, unknown } for a proposed change.

	``unknown`` is true when no provider could evaluate the proposed tree at all —
	the counts are then both zero, which is what the sync really does, but zero
	there means "nothing can be worked out", not "nothing matches". It is a new
	field rather than a new shape for ``removed``: the three original keys are what
	the rules panel reads, and a consumer that ignores ``unknown`` still sees counts
	that agree with the sync.

	Channels only. A workspace has no rules to propose a change to — the way to move
	its membership is to change a channel's, and this endpoint reports that already."""
	_require_manager()
	target_doctype = _choice(
		target_doctype, "target_doctype", {"Raven Channel Mapping"}, _("Unsupported sync target")
	)
	name = _str(name, "name")
	_require_mapping(target_doctype, name)
	if new_rules is None:
		new_rules = frappe.parse_json(
			frappe.db.get_value(target_doctype, name, "member_rules_json") or "null"
		)
	# Previewing a diff saves nothing, so an unnamed rule is not an error here.
	new_tree = _rule_tree(new_rules, require_label=False)

	from raven_integration.engine import (
		disabled_rule_members,
		evaluate_rules_or_unknown,
		has_active_rules,
	)

	# Mirror sync_service exactly, or the confirmation lies. It compares the rules
	# against who is *actually* rule-managed right now, not against the population
	# the old rules would produce, and it honours the same guards: a channel that is
	# switched off or whose Raven channel was deleted is skipped before any rule is
	# read, a rule set with nothing active is skipped wholesale, and disabled rules
	# freeze rather than evict. Every one of those means "nobody moves", which a
	# naive set difference over the rules alone reports as removing everyone.
	no_change = {"added": 0, "removed": 0, "removed_users": [], "unknown": False}
	mapping = frappe.db.get_value(target_doctype, name, ["enabled", "stale", "raven_channel"], as_dict=True)
	if not mapping.enabled or mapping.stale:
		return no_change
	if not has_active_rules(new_tree):
		return no_change

	current = (
		set(
			frappe.get_all(
				"Raven Channel Member",
				filters={"channel_id": mapping.raven_channel, "added_by_rule": 1},
				pluck="user_id",
			)
		)
		if mapping.raven_channel
		else set()
	)

	new_set = evaluate_rules_or_unknown(new_tree, strict=False)
	if new_set is None:
		# Nothing in the tree could be evaluated — the provider's app is gone, say.
		# The sync runs strict, so it raises and moves nobody; treating the empty
		# lenient answer as authoritative would announce that every rule-managed
		# member is about to be removed by a sync that will not remove one of them.
		return {"added": 0, "removed": 0, "removed_users": [], "unknown": True}

	frozen = current & disabled_rule_members(new_tree, strict=False)
	added = new_set - current
	removed = current - new_set - frozen

	return {
		"added": len(added),
		"removed": len(removed),
		"removed_users": sorted(removed)[:10],
		"unknown": False,
	}


@frappe.whitelist()
def list_unmapped_workspaces() -> list[dict]:
	"""Raven Workspaces that no Raven Workspace Mapping manages yet.

	Uses frappe.qb with a NOT IN over the mappings' non-null raven_workspace links
	(specs/security.md §2 — no raw SQL)."""
	_require_manager()
	RW = frappe.qb.DocType("Raven Workspace")
	RWM = frappe.qb.DocType("Raven Workspace Mapping")
	mapped = frappe.qb.from_(RWM).select(RWM.raven_workspace).where(RWM.raven_workspace.isnotnull())
	return (
		frappe.qb.from_(RW)
		.select(RW.name, RW.workspace_name, RW.type)
		.where(RW.name.notin(mapped))
		.orderby(RW.workspace_name)
		.run(as_dict=True)
	)


@frappe.whitelist()
def link_workspace(raven_workspace: str) -> str:
	"""Adopt an existing Raven Workspace into a new Raven Workspace Mapping.

	No new Raven Workspace is created: flags.skip_raven_create suppresses the
	before_insert provisioning and the mapping is pointed at the supplied id. The
	unique raven_workspace constraint (not an exists-then-insert) enforces one
	mapping per Raven workspace — a second attempt surfaces as 'already managed'.
	Returns the new mapping's docname."""
	_require_manager()
	raven_workspace = _str(raven_workspace, "raven_workspace")

	ws = frappe.db.get_value("Raven Workspace", raven_workspace, ["workspace_name", "type"], as_dict=True)
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
	doc.raven_workspace = raven_workspace
	doc.flags.skip_raven_create = True
	try:
		doc.insert()
	except frappe.DuplicateEntryError:
		# db_insert msgprints "… already exists" under a red "Duplicate Name" before
		# raising; left in place the user gets that dialog as well as this one.
		frappe.clear_last_message()
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
	_require_manager()
	workspace = _str(workspace, "workspace")
	_require_mapping("Raven Workspace Mapping", workspace)
	raven_workspace = frappe.db.get_value("Raven Workspace Mapping", workspace, "raven_workspace")
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
	rules: "dict | None" = None,
) -> str:
	"""Adopt an existing Raven Channel into a new Raven Channel Mapping under ``workspace``.

	``workspace`` is the parent Raven Workspace Mapping name. No new Raven Channel is
	created: flags.skip_raven_create suppresses provisioning and the mapping is
	pointed at the supplied id. The unique raven_channel constraint enforces one
	mapping per Raven channel — a second attempt surfaces as 'already managed'.
	Returns the new mapping's docname."""
	_require_manager()
	workspace = _str(workspace, "workspace")
	raven_channel = _str(raven_channel, "raven_channel")
	rule_tree = _rule_tree(rules)

	_require_mapping("Raven Workspace Mapping", workspace)
	ch = frappe.db.get_value(
		"Raven Channel",
		raven_channel,
		["channel_name", "type", "workspace", "is_direct_message", "is_thread"],
		as_dict=True,
	)
	if not ch:
		frappe.throw(
			title=_("Channel not found"),
			msg=_(
				"No Raven Channel named <b>{0}</b> exists. Reload the page and pick a channel from the list."
			).format(escape_html(raven_channel)),
			exc=frappe.DoesNotExistError,
		)
	# The same two checks list_unmapped_channels applies to decide what is offered.
	# Without them the id can be posted directly: a direct message adopted here has
	# rule-matched strangers inserted into a two-person conversation, and a channel
	# from another Raven workspace joins its members to this mapping's workspace
	# while on_trash evicts them against the one the channel actually lives in.
	if ch.is_direct_message or ch.is_thread:
		frappe.throw(
			title=_("This channel cannot be managed"),
			msg=_(
				"<b>{0}</b> is a direct message or a thread, and membership rules would "
				"add people to a private conversation. Pick a regular channel from the list."
			).format(escape_html(ch.channel_name or raven_channel)),
		)
	raven_workspace = frappe.db.get_value("Raven Workspace Mapping", workspace, "raven_workspace")
	if not raven_workspace or ch.workspace != raven_workspace:
		frappe.throw(
			title=_("Channel is in another workspace"),
			msg=_(
				"<b>{0}</b> lives in Raven workspace <b>{1}</b>, not in <b>{2}</b>. "
				"Open that workspace and adopt the channel there."
			).format(
				escape_html(ch.channel_name or raven_channel),
				escape_html(ch.workspace or _("none")),
				escape_html(raven_workspace or _("none")),
			),
		)

	doc = frappe.new_doc("Raven Channel Mapping")
	doc.channel_label = ch.channel_name
	doc.workspace = workspace
	doc.channel_type = ch.type if ch.type in _VALID_CH_TYPES else "Private"
	doc.member_rules_json = frappe.as_json(rule_tree)
	doc.raven_channel = raven_channel
	doc.flags.skip_raven_create = True
	try:
		doc.insert()
	except frappe.DuplicateEntryError:
		# Same stale red "Duplicate Name" dialog as link_workspace clears.
		frappe.clear_last_message()
		frappe.throw(
			title=_("Channel already managed"),
			msg=_(
				"<b>{0}</b> is already linked to a Raven Channel Mapping. "
				"Reload the page — it should already appear in the list."
			).format(escape_html(raven_channel)),
		)
	return doc.name
