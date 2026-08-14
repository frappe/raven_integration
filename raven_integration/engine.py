from __future__ import annotations

import json

import frappe
from frappe import _

from raven_integration.exceptions import ProviderDataError
from raven_integration.registry import evaluate as registry_evaluate
from raven_integration.registry import validate_rule_config
from raven_integration.utils import raven_installed

RULE_COMBINATOR_OR = "Any (OR)"
RULE_COMBINATOR_AND = "All (AND)"


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


def _rule_identity(rule: dict) -> tuple:
	"""Identity of a rule for duplicate detection: provider + rule_type + config.

	config is normalized through json.dumps(sort_keys=True) so key order /
	string-vs-dict storage don't matter.
	"""
	return (
		rule.get("provider"),
		rule.get("rule_type"),
		json.dumps(_parse_config(rule), sort_keys=True),
	)


def validate_unique_member_rules(rules: list) -> None:
	"""Throw if any two member rules are identical (same provider/rule_type/config)."""
	seen: set[tuple] = set()
	for r in rules:
		identity = _rule_identity(_normalize_rule(r))
		if identity in seen:
			frappe.throw(
				_(
					"Duplicate membership rule: a rule with the same type and settings already exists on this record."
				),
				title=_("Duplicate Rule"),
			)
		seen.add(identity)


def validate_member_rules(rules: list) -> None:
	"""Validate each rule's config against its provider, then reject duplicates."""
	for rule in rules:
		validate_rule_config(rule.provider, rule.rule_type, rule.config)
	validate_unique_member_rules(rules)


def has_active_rules(rules: list) -> bool:
	"""True if any rule row is not Paused.

	Distinguishes "no opinion" (no active rules) from "matches nobody" (active
	rules that evaluate to an empty population). Only the second is authoritative:
	treating the first as authoritative would make deleting or pausing the last
	rule evict every rule-managed member of the mapping.
	"""
	return any(not _is_paused(_normalize_rule(r)) for r in rules)


def disabled_rule_members(rules: list, *, strict: bool = True) -> set[str]:
	"""Population of the Paused rules (the UI calls them Disabled).

	Disabling a rule stops it granting membership to anyone new, but must not evict
	the people it already added — callers freeze ``current & this`` rather than
	removing them. Intersecting with the current membership is what makes it
	"adds nobody new": a person who only newly matches a disabled rule isn't in
	``current``, so they are never added.

	Always a union, even under All (AND): a disabled rule is a frozen contribution,
	not a filter. Disable is only offered on Any (OR) mappings for that reason —
	under AND a rule narrows the population, so dropping it would *add* people.
	"""
	out: set[str] = set()
	for r in rules:
		rd = _normalize_rule(r)
		if not _is_paused(rd):
			continue
		try:
			out |= _evaluate_one(rd)
		except (ProviderDataError, frappe.ValidationError):
			if strict:
				raise
			frappe.log_error(
				title="Raven Integration: skipped disabled rule the provider could not evaluate",
				message=(
					f"Disabled rule {rd.get('name')!r} on {rd.get('parenttype')} "
					f"{rd.get('parent')!r} could not be evaluated; its members are "
					f"not frozen and may be removed by the next sync."
				),
			)
	return out


def _evaluate_one(rule: dict) -> set[str]:
	"""Dispatch a single rule to its provider via the registry."""
	return registry_evaluate(rule.get("provider"), rule.get("rule_type"), _parse_config(rule))


def evaluate_rules(rules: list, *, strict: bool = True, combinator: str = RULE_COMBINATOR_OR) -> set[str]:
	"""Combine per-rule populations across every active rule.

	When strict is False, a rule the provider cannot evaluate is logged and
	skipped instead of raising (read/display path); the sync path keeps the
	default strict=True so unexpected data fails loud.
	"""
	sets: list[set[str]] = []
	for r in rules:
		rd = _normalize_rule(r)
		if _is_paused(rd):
			continue
		try:
			sets.append(_evaluate_one(rd))
		except (ProviderDataError, frappe.ValidationError):
			if strict:
				raise
			frappe.log_error(
				title="Raven Integration: skipped rule the provider could not evaluate",
				message=(
					f"Rule {rd.get('name')!r} on {rd.get('parenttype')} "
					f"{rd.get('parent')!r} (provider={rd.get('provider')!r}, "
					f"rule_type={rd.get('rule_type')!r}) could not be evaluated; "
					f"skipped in read-path evaluation."
				),
			)
	if not sets:
		return set()
	if combinator == RULE_COMBINATOR_AND:
		return set.intersection(*sets)
	return set.union(*sets)


def expected_channel_members(
	channel_name: str, *, strict: bool = True, cache: dict | None = None
) -> set[str]:
	"""Channel members = its own rules, combined by its combinator.

	Nothing narrows this. A channel does not sit inside a population the workspace
	defines — it *is* where membership is defined, and the workspace's own membership
	is derived back out of it (see expected_workspace_members). ``cache`` is accepted
	for signature symmetry with the workspace side and is unused: a channel's rules
	are evaluated once per sweep already.
	"""
	channel = frappe.get_doc("Raven Channel Mapping", channel_name)
	return evaluate_rules(
		channel.member_rules,
		strict=strict,
		combinator=channel.rule_combinator or RULE_COMBINATOR_OR,
	)


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
