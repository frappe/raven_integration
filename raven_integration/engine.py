from __future__ import annotations

import json

import frappe
from frappe import _

from raven_integration.exceptions import ProviderDataError
from raven_integration.registry import evaluate as registry_evaluate
from raven_integration.registry import validate_rule_config
from raven_integration.utils import raven_installed

CONJUNCTION_AND = "and"
CONJUNCTION_OR = "or"

# How deep a stored tree may nest, and how many nodes it may hold. Mirrors the
# frontend component's default maxDepth. Enforced on the write path; the read path
# evaluates whatever is stored, so a tree that predates a lowered cap still works.
MAX_TREE_DEPTH = 4
MAX_TREE_NODES = 200


def _normalize_rule(rule) -> dict:
	"""Convert a child Document row to a plain dict; pass dicts through unchanged."""
	return rule if isinstance(rule, dict) else rule.as_dict()


def _is_paused(rule: dict) -> bool:
	"""True if this rule row is disabled (stored as status 'Paused')."""
	return rule.get("status") == "Paused"


def _parse_config(rule: dict) -> dict:
	"""Rule config as a dict; unparseable payloads degrade to {} rather than raising."""
	config = rule.get("config")
	if isinstance(config, str):
		try:
			config = json.loads(config)
		except (ValueError, TypeError):
			config = {}
	return config or {}


def is_group(node) -> bool:
	"""True if ``node`` is a group rather than a leaf.

	Structural, with no discriminator field to keep in sync: a group is anything
	carrying both a conjunction list and a condition list. Same test the frontend
	component applies, so a tree means the same thing on both sides.
	"""
	return isinstance(node, dict) and "conditions" in node and "conjunctions" in node


def parse_tree(value) -> "dict | None":
	"""Read a stored ``member_rules_json`` into a root group, or None if there is none.

	Accepts the parsed dict as readily as the string the DB holds. Anything that is
	not a group — null, an empty string, a legacy list, a corrupted payload — reads
	as "no rules", which is what an empty mapping has always meant. Callers are
	read paths that must not raise on stored data; the write path validates instead.
	"""
	if isinstance(value, str):
		try:
			value = json.loads(value) if value.strip() else None
		except (ValueError, TypeError):
			return None
	return value if is_group(value) else None


def iter_leaves(tree, _path: "list[int] | None" = None):
	"""Yield ``(path, leaf)`` for every leaf of ``tree``, depth first.

	The path is the child indices from the root, which is how a leaf is addressed
	now that rules have no docnames.
	"""
	if not is_group(tree):
		return
	path = _path or []
	for index, node in enumerate(tree.get("conditions") or []):
		if is_group(node):
			yield from iter_leaves(node, [*path, index])
		elif isinstance(node, dict):
			yield [*path, index], node


def validate_member_rules(tree) -> None:
	"""Validate every leaf's config and hold the pause rule."""
	for path, leaf in iter_leaves(tree):
		# The registry returns early on an empty provider or rule type, because a
		# child row's own reqd fields used to catch that. A leaf of a JSON tree has no
		# such check behind it, so an unfinished condition would save silently and
		# then match nobody for reasons nothing on the page explains.
		if not leaf.get("provider") or not leaf.get("rule_type"):
			frappe.throw(
				title=_("Unfinished condition"),
				msg=_("A condition has no type chosen yet. Finish it or remove it, then save again."),
			)
		validate_rule_config(leaf.get("provider"), leaf.get("rule_type"), leaf.get("config"))
		if _is_paused(leaf) and not pausable(tree, path):
			frappe.throw(
				title=_("A paused condition sits under “and”"),
				msg=_(
					"Pausing freezes who a condition already added instead of dropping it, which "
					"only holds while the condition <i>adds</i> people. Under <b>and</b> it narrows "
					"its group instead, so pausing it would add people. Resume it, remove it, or "
					"join its group with <b>or</b>."
				),
			)


def pausable(tree, path: list) -> bool:
	"""True if the leaf at ``path`` may be paused.

	Pausing freezes a rule's contribution instead of dropping it, which only reads
	as "adds nobody new" while the rule *adds* people. Under ``and`` a rule narrows,
	so dropping it would grow the population — hence pausing is offered only where
	every group between the leaf and the root joins its children with ``or``.
	"""
	node = tree
	for index in path:
		if not is_group(node):
			return False
		if CONJUNCTION_AND in (node.get("conjunctions") or []):
			return False
		conditions = node.get("conditions") or []
		if index >= len(conditions):
			return False
		node = conditions[index]
	return not is_group(node)


def has_active_rules(tree) -> bool:
	"""True if any leaf of the tree is not Paused.

	Distinguishes "no opinion" (no active rules) from "matches nobody" (active
	rules that evaluate to an empty population). Only the second is authoritative:
	treating the first as authoritative would make deleting or pausing the last
	rule evict every rule-managed member of the mapping.
	"""
	return any(not _is_paused(leaf) for _path, leaf in iter_leaves(parse_tree(tree)))


def disabled_rule_members(tree, *, strict: bool = True) -> set[str]:
	"""Population of the Paused rules (the UI calls them Disabled).

	Disabling a rule stops it granting membership to anyone new, but must not evict
	the people it already added — callers freeze ``current & this`` rather than
	removing them. Intersecting with the current membership is what makes it
	"adds nobody new": a person who only newly matches a disabled rule isn't in
	``current``, so they are never added.

	Always a union, whatever conjunction joins them: a disabled rule is a frozen
	contribution, not a filter. Pausing is only offered where every group above the
	leaf joins with ``or`` for that reason — under ``and`` a rule narrows the
	population, so dropping it would *add* people. See ``pausable``.
	"""
	out: set[str] = set()
	skipped: dict = {}
	for path, leaf in iter_leaves(parse_tree(tree)):
		if not _is_paused(leaf):
			continue
		messages_before = len(frappe.message_log)
		try:
			out |= _evaluate_one(leaf)
		except (ProviderDataError, frappe.ValidationError) as e:
			if strict:
				raise
			_record_skipped(skipped, path, leaf, e, messages_before)
	_log_skipped(
		skipped,
		title="Raven Integration: skipped disabled rules the provider could not evaluate",
		consequence="Their members are not frozen and may be removed by the next sync.",
	)
	return out


def _record_skipped(skipped: dict, path: list, leaf: dict, error: Exception, messages_before: int) -> None:
	"""Swallow one unevaluable leaf: unqueue its dialog, remember it for the log.

	frappe.msgprint appends to frappe.message_log before frappe.throw raises, so
	catching the exception without dropping the message returns HTTP 200 carrying
	_server_messages and the settings page pops a red error dialog on a request that
	succeeded. Only what this leaf queued is dropped — a ProviderDataError queues
	nothing, and the caller's own messages are not ours to discard.
	"""
	while len(frappe.message_log) > messages_before:
		frappe.clear_last_message()
	skipped.setdefault((leaf.get("provider"), leaf.get("rule_type"), str(error)), []).append(path)


def _log_skipped(skipped: dict, *, title: str, consequence: str) -> None:
	"""One Error Log per call for everything it skipped, rather than one per leaf.

	The lenient path serves a read endpoint over a tree that may hold MAX_TREE_NODES
	leaves, so an uninstalled provider app used to insert an Error Log document per
	leaf, on every reload. Leaves failing for the same provider, rule type and reason
	are one finding; their paths are kept so each one can still be found.
	"""
	if not skipped:
		return
	blocks = [
		f"provider={provider!r} rule_type={rule_type!r}: {error}\n"
		f"{consequence}\nAffected rules ({len(paths)}) at: {paths}"
		for (provider, rule_type, error), paths in skipped.items()
	]
	frappe.log_error(title=title, message="\n\n".join(blocks))


def _evaluate_one(rule: dict) -> set[str]:
	"""Dispatch a single rule to its provider via the registry."""
	return registry_evaluate(rule.get("provider"), rule.get("rule_type"), _parse_config(rule))


def _evaluate_node(node, path: list, *, strict: bool, skipped: dict) -> "set[str] | None":
	"""One node's population, or None when the node contributes nothing at all.

	Absent is not the same as empty. A paused leaf, a leaf the provider could not
	evaluate on the lenient path, and a group with no surviving children are all
	*absent*: they drop out of their parent's fold entirely, so an ``and`` beside
	them does not intersect the population down to nobody. A rule that evaluates
	honestly to no users is empty, and does narrow an ``and``.
	"""
	if is_group(node):
		return _evaluate_group(node, path, strict=strict, skipped=skipped)
	if not isinstance(node, dict) or _is_paused(node):
		return None
	messages_before = len(frappe.message_log)
	try:
		return _evaluate_one(node)
	except (ProviderDataError, frappe.ValidationError) as e:
		if strict:
			raise
		_record_skipped(skipped, path, node, e, messages_before)
		return None


def _evaluate_group(group: dict, path: list, *, strict: bool, skipped: dict) -> "set[str] | None":
	"""Fold a group's children by its conjunctions, or None if none survive.

	The fold applies classic precedence — ``and`` binds tighter than ``or`` — so a
	level mixing the two means what it reads like: ``A and B or C and D`` is
	``(A and B) or (C and D)``, not a left-to-right fold. Frappe Learning's editor
	writes one conjunction per group, so a mixed level only ever arrives through
	this API.

	The and-runs are marked out from the *stored* conjunctions, before anything is
	dropped, and an absent child then leaves its run rather than shifting the gaps
	behind it. That is the whole point: in ``A or B and C`` with B absent, letting C
	inherit B's gap would answer ``A and C`` — narrower than A alone. Skipping a rule
	may leave the population as it was or widen it, never shrink it, which is the
	promise ``pausable`` and the lenient read path both rest on.
	"""
	conditions = group.get("conditions") or []
	conjunctions = group.get("conjunctions") or []
	or_terms: list[set[str]] = []
	run: "set[str] | None" = None
	for index, child in enumerate(conditions):
		# The gap before this child; the first child starts the first run and has none.
		joiner = conjunctions[index - 1] if 0 < index <= len(conjunctions) else CONJUNCTION_OR
		if index and joiner != CONJUNCTION_AND:
			if run is not None:
				or_terms.append(run)
			run = None
		value = _evaluate_node(child, [*path, index], strict=strict, skipped=skipped)
		if value is None:
			continue
		run = value if run is None else run & value
	if run is not None:
		or_terms.append(run)
	if not or_terms:
		return None
	return set().union(*or_terms)


def evaluate_rules(tree, *, strict: bool = True) -> set[str]:
	"""Population of a whole condition tree.

	When strict is False, a rule the provider cannot evaluate is logged and skipped
	instead of raising (read/display path); the sync path keeps the default
	strict=True so unexpected data fails loud.

	A tree that yielded no opinion at all reads as the empty set here, deliberately:
	these callers are asking who to hold membership open for, and every one of them
	wants "nobody" for a tree that says nothing. A caller that must tell "matches
	nobody" from "could not be evaluated" — a diff preview, where the difference is
	between removing nobody and removing everyone — calls evaluate_rules_or_unknown.
	"""
	result = evaluate_rules_or_unknown(tree, strict=strict)
	return result if result is not None else set()


def evaluate_rules_or_unknown(tree, *, strict: bool = False) -> "set[str] | None":
	"""Population of a whole condition tree, or None when it yielded no opinion.

	None is what _evaluate_node calls absent: every leaf paused, skipped on the
	lenient path, or no leaves at all. It is not the empty set, which is a tree that
	was evaluated and honestly matches nobody. Lenient by default, because a caller
	that wanted a raise would have no use for the distinction.
	"""
	skipped: dict = {}
	result = _evaluate_node(parse_tree(tree), [], strict=strict, skipped=skipped)
	_log_skipped(
		skipped,
		title="Raven Integration: skipped rules the provider could not evaluate",
		consequence="Skipped in read-path evaluation.",
	)
	return result


def expected_channel_members(
	channel_name: str, *, strict: bool = True, cache: dict | None = None
) -> set[str]:
	"""Channel members = the population of its own condition tree.

	Nothing narrows this. A channel does not sit inside a population the workspace
	defines — it *is* where membership is defined, and the workspace's own membership
	is derived back out of it (see expected_workspace_members). ``cache`` is accepted
	for signature symmetry with the workspace side and is unused: a channel's rules
	are evaluated once per sweep already.
	"""
	channel = frappe.get_doc("Raven Channel Mapping", channel_name)
	return evaluate_rules(channel.member_rules_json, strict=strict)


def expected_workspace_members(workspace_name: str, *, cache: dict | None = None) -> set[str]:
	"""Workspace members = whoever is in at least one channel of the workspace.

	Derived, never ruled: adding someone to a channel puts them in the workspace,
	and losing their last channel takes them back out (only ever a row this app
	added — the added_by_rule guard in sync_service is what enforces that).

	Read from the channels' *actual* Raven membership rather than from their rules,
	so a channel this app is not syncing — disabled, stale, or never mapped at all —
	still holds its members in the workspace. resync_all sweeps channels before
	workspaces for exactly this reason: by the time a workspace is diffed, its
	channels already hold the membership their rules ask for.
	"""
	if cache is not None and workspace_name in cache:
		return cache[workspace_name]
	raven_workspace = frappe.db.get_value("Raven Workspace Mapping", workspace_name, "raven_workspace")
	result = members_of_workspace_channels(raven_workspace) if raven_workspace else set()
	if cache is not None:
		cache[workspace_name] = result
	return result


def members_of_workspace_channels(raven_workspace: str) -> set[str]:
	"""Every user holding a membership row in any channel of a Raven workspace.

	Direct-message and thread channels count too. Excluding them could only ever
	*evict* someone Raven still shows a conversation to, and this app's one hard
	rule is that it never removes what it did not add.
	"""
	if not raven_installed():
		return set()
	RC = frappe.qb.DocType("Raven Channel")
	RCM = frappe.qb.DocType("Raven Channel Member")
	return set(
		frappe.qb.from_(RCM)
		.join(RC)
		.on(RC.name == RCM.channel_id)
		.select(RCM.user_id)
		.distinct()
		.where(RC.workspace == raven_workspace)
		.run(pluck=True)
	)
