from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import escape_html

_REQUIRED_PROVIDER_KEYS = ("name", "evaluate", "rule_types")


def _provider_paths() -> list[str]:
	return list(frappe.get_hooks("raven_membership_providers") or [])


def _validate_provider_declaration(provider, path: str) -> None:
	"""Reject a declaration the registry cannot use, naming the hook that produced it."""
	if not isinstance(provider, dict):
		frappe.throw(
			title=_("Invalid membership provider"),
			msg=_(
				"<b>{0}</b> returned {1} instead of a provider declaration. "
				"A <code>raven_membership_providers</code> hook must return a dict with "
				"the keys {2}. Fix that function in the app that registers it, then reload."
			).format(escape_html(path), type(provider).__name__, ", ".join(_REQUIRED_PROVIDER_KEYS)),
		)
	missing = [key for key in _REQUIRED_PROVIDER_KEYS if key not in provider]
	if missing:
		frappe.throw(
			title=_("Invalid membership provider"),
			msg=_(
				"The membership provider declared by <b>{0}</b> is missing the required "
				"key(s) <b>{1}</b>. A declaration must define {2}. Add the missing key(s) "
				"in the app that registers this hook, then reload."
			).format(
				escape_html(path),
				", ".join(missing),
				", ".join(_REQUIRED_PROVIDER_KEYS),
			),
		)
	if not isinstance(provider["name"], str) or not provider["name"].strip():
		frappe.throw(
			title=_("Invalid membership provider"),
			msg=_(
				"The membership provider declared by <b>{0}</b> has a <b>name</b> of {1}. "
				"A provider name must be a non-empty string — it is what rules are stored "
				"against. Fix it in the app that registers this hook, then reload."
			).format(escape_html(path), type(provider["name"]).__name__),
		)
	if not callable(provider["evaluate"]):
		frappe.throw(
			title=_("Invalid membership provider"),
			msg=_(
				"The membership provider <b>{0}</b> declared by <b>{1}</b> has an "
				"<b>evaluate</b> of {2}. It must be a callable taking (rule_type, config) "
				"and returning the matching users. Fix it in the app that registers this "
				"hook, then reload."
			).format(escape_html(provider["name"]), escape_html(path), type(provider["evaluate"]).__name__),
		)
	if not isinstance(provider["rule_types"], list):
		frappe.throw(
			title=_("Invalid membership provider"),
			msg=_(
				"The membership provider <b>{0}</b> declared by <b>{1}</b> has "
				"<b>rule_types</b> of {2}. It must be a list of rule-type declarations. "
				"Fix it in the app that registers this hook, then reload."
			).format(
				escape_html(provider["name"]),
				escape_html(path),
				type(provider["rule_types"]).__name__,
			),
		)


def _throw_duplicate_provider(name: str, first_path: str, second_path: str) -> None:
	frappe.throw(
		title=_("Duplicate membership provider name"),
		msg=_(
			"Two apps register a membership provider named <b>{0}</b>:<br>"
			"&bull; {1}<br>&bull; {2}<br>"
			"Provider names identify who evaluates a rule, so only one of them could win "
			"and the other app's rules would stop being evaluated. Change the <b>name</b> "
			"in one of these declarations, or uninstall one of the two apps."
		).format(escape_html(name), escape_html(first_path), escape_html(second_path)),
	)


def _load_providers() -> dict[str, dict]:
	out: dict[str, dict] = {}
	paths: dict[str, str] = {}
	for path in _provider_paths():
		provider = frappe.get_attr(path)()
		_validate_provider_declaration(provider, path)
		name = provider["name"]
		if name in out:
			_throw_duplicate_provider(name, paths[name], path)
		paths[name] = path
		out[name] = provider
	return out


def trigger_doctypes() -> set[str]:
	"""Doctypes any registered provider wants to react to (from provider['triggers'])."""
	out: set[str] = set()
	for provider in _load_providers().values():
		for trigger in provider.get("triggers", []) or []:
			out.add(trigger["doctype"] if isinstance(trigger, dict) else trigger)
	return out


def list_providers() -> list[dict]:
	# UI-facing: strip the callable
	return [
		{
			"name": p["name"],
			"label": p.get("label", p["name"]),
			"rule_types": p.get("rule_types", []),
		}
		for p in _load_providers().values()
	]


def _get_provider(provider: str) -> dict:
	p = _load_providers().get(provider)
	if not p:
		frappe.throw(_("Unknown membership provider: {0}").format(provider))
	return p


def _get_rule_type(provider_decl: dict, provider: str, rule_type: str) -> dict:
	"""The provider's declaration of rule_type, or throw if it declares no such type.

	Takes the already-resolved declaration rather than the provider name so a
	caller that needs both does not reload every app's providers twice.
	"""
	decl = next((rt for rt in provider_decl.get("rule_types", []) if rt["type"] == rule_type), None)
	if decl is None:
		frappe.throw(_("Provider {0} has no rule type {1}").format(provider, rule_type))
	return decl


def list_rule_types(provider: str) -> list[dict]:
	return _get_provider(provider).get("rule_types", [])


def evaluate(provider: str, rule_type: str, config: dict) -> set[str]:
	p = _get_provider(provider)
	_get_rule_type(p, provider, rule_type)
	return set(p["evaluate"](rule_type, config or {}))


def validate_rule_config(provider: str, rule_type: str, config) -> None:
	if not provider or not rule_type:
		return  # mandatory check handles emptiness
	decl = _get_rule_type(_get_provider(provider), provider, rule_type)
	cfg = json.loads(config) if isinstance(config, str) else (config or {})
	for f in decl.get("fields", []):
		if f.get("reqd") and not cfg.get(f["fieldname"]):
			frappe.throw(
				_("Rule field '{0}' is required for {1}").format(f.get("label", f["fieldname"]), rule_type)
			)
