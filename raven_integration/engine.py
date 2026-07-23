from __future__ import annotations

import json

import frappe
from frappe import _

from raven_integration.exceptions import ProviderDataError
from raven_integration.registry import evaluate as registry_evaluate
from raven_integration.registry import validate_rule_config

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
				_("Duplicate membership rule: a rule with the same type and settings already exists on this record."),
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


def evaluate_rules(
	rules: list, *, strict: bool = True, combinator: str = RULE_COMBINATOR_OR
) -> set[str]:
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
	"""Channel members = (its rules combined by its combinator) ∩ workspace members.

	A workspace with no active rules expresses no opinion about who belongs, so it
	imposes no constraint here — intersecting with its empty population would wipe
	the channel instead.
	"""
	channel = frappe.get_doc("Raven Channel Mapping", channel_name)
	own = evaluate_rules(
		channel.member_rules,
		strict=strict,
		combinator=channel.rule_combinator or RULE_COMBINATOR_OR,
	)
	ws = frappe.get_doc("Raven Workspace Mapping", channel.workspace)
	if not has_active_rules(ws.member_rules):
		return own
	return own & expected_workspace_members(channel.workspace, strict=strict, cache=cache)


def expected_workspace_members(
	workspace_name: str, *, strict: bool = True, cache: dict | None = None
) -> set[str]:
	"""Workspace members = its own rules combined by its combinator."""
	if cache is not None and (workspace_name, strict) in cache:
		return cache[(workspace_name, strict)]
	ws = frappe.get_doc("Raven Workspace Mapping", workspace_name)
	result = evaluate_rules(
		ws.member_rules,
		strict=strict,
		combinator=ws.rule_combinator or RULE_COMBINATOR_OR,
	)
	if cache is not None:
		cache[(workspace_name, strict)] = result
	return result
