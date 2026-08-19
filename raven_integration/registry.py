from __future__ import annotations

import json
from collections.abc import Iterable

import frappe
from frappe import _
from frappe.utils import escape_html

from raven_integration.exceptions import ProviderDataError

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

	_validate_rule_types(provider["name"], provider["rule_types"], path)


def _throw_declaration(provider: str, path: str, msg: str) -> None:
	frappe.throw(
		title=_("Invalid membership provider"),
		msg=_(
			"The membership provider <b>{0}</b> declared by <b>{1}</b> {2} Fix it in the app that registers this hook, then reload."
		).format(escape_html(provider), escape_html(path), msg),
	)


def _validate_rule_types(provider: str, rule_types: list, path: str) -> None:
	"""Reject a rule-type declaration the registry or the rule builder cannot use.

	`fields` is a schema two sides read — `validate_rule_config` here and the host's
	form renderer there — so a key either side dereferences is checked once, at load,
	where the message can name the hook that produced it. `depends_on` is the one that
	most needs it: it is dereferenced as a dict, and a fieldname it names that the
	rule type does not declare hides that field on every row, forever, with nothing
	said. Both used to be discovered as an AttributeError out of a channel save.
	"""
	for rt in rule_types:
		if not isinstance(rt, dict):
			_throw_declaration(
				provider, path, _("declares a rule type of {0} instead of a dict.").format(type(rt).__name__)
			)
		type_name = rt.get("type")
		if not isinstance(type_name, str) or not type_name.strip():
			_throw_declaration(
				provider,
				path,
				_(
					"declares a rule type whose <b>type</b> is {0}. It must be a non-empty string — it is what rules are stored against."
				).format(type(type_name).__name__),
			)
		fields = rt.get("fields") or []
		if not isinstance(fields, list):
			_throw_declaration(
				provider,
				path,
				_(
					"declares rule type <b>{0}</b> with <b>fields</b> of {1}. It must be a list of field declarations."
				).format(escape_html(type_name), type(fields).__name__),
			)
		fieldnames = set()
		for f in fields:
			if not isinstance(f, dict):
				_throw_declaration(
					provider,
					path,
					_("declares a field of {0} on rule type <b>{1}</b> instead of a dict.").format(
						type(f).__name__, escape_html(type_name)
					),
				)
			fieldname = f.get("fieldname")
			if not isinstance(fieldname, str) or not fieldname.strip():
				_throw_declaration(
					provider,
					path,
					_(
						"declares a field on rule type <b>{0}</b> whose <b>fieldname</b> is {1}. It must be a non-empty string — it is the key the rule's config is stored under."
					).format(escape_html(type_name), type(fieldname).__name__),
				)
			fieldnames.add(fieldname)
		for f in fields:
			dep = f.get("depends_on")
			if dep is None:
				continue
			if not isinstance(dep, dict):
				_throw_declaration(
					provider,
					path,
					_(
						"declares <b>depends_on</b> as {0} on field <b>{1}</b> of rule type <b>{2}</b>. It must be a dict carrying a <b>field</b> key and a <b>value</b> key; the <code>eval:</code> string form Frappe accepts on a DocField is not supported here."
					).format(type(dep).__name__, escape_html(f["fieldname"]), escape_html(type_name)),
				)
			if dep.get("field") not in fieldnames:
				_throw_declaration(
					provider,
					path,
					_(
						"declares <b>depends_on.field</b> of {0} on field <b>{1}</b> of rule type <b>{2}</b>, which that rule type does not declare. The field would be hidden on every rule and never rendered."
					).format(
						escape_html(str(dep.get("field"))),
						escape_html(f["fieldname"]),
						escape_html(type_name),
					),
				)
			if "value" not in dep:
				_throw_declaration(
					provider,
					path,
					_(
						"declares <b>depends_on</b> with no <b>value</b> on field <b>{0}</b> of rule type <b>{1}</b>. Name the value of <b>{2}</b> that this field applies to."
					).format(
						escape_html(f["fieldname"]),
						escape_html(type_name),
						escape_html(str(dep.get("field"))),
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


def _members_returned(value, provider: str, rule_type: str) -> set[str]:
	"""The users a provider's evaluate returned, or ProviderDataError naming who broke.

	set() is not a type check, and a provider is another app's code. A returned
	string becomes one member per character, which sync then adds one character at a
	time until one of them fails — after earlier adds have already landed. A returned
	None raises TypeError, which the engine's lenient handler does not catch, so a
	read of the channel page 500s. ProviderDataError instead, because both the strict
	sync path and the lenient read path already know what to do with it.

	Any iterable is accepted, as the contract in the README promises: a provider that
	answers with a generator, a ``dict_keys`` or a QueryBuilder result is returning
	users, and narrowing this to four concrete collections would stop its channels
	syncing over the shape of the container. ``str`` and ``bytes`` are the two
	iterables that are excluded, because they are the ones that iterate into
	characters rather than into users.
	"""
	if value is None or isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
		raise ProviderDataError(
			f"Membership provider {provider!r} returned {type(value).__name__} for rule type "
			f"{rule_type!r}. evaluate(rule_type, config) must return an iterable of user IDs "
			f"(a set, list, tuple or generator — not a string). Fix that provider in the app "
			f"that registers it."
		)
	# Drained once, before the check below: a generator is consumed by iterating it,
	# so validating first and converting after would hand back an empty set.
	try:
		members = set(value)
	except TypeError as e:
		raise ProviderDataError(
			f"Membership provider {provider!r} returned unhashable entries among the users "
			f"matching rule type {rule_type!r} ({e}). Every entry must be a user ID string. "
			f"Fix that provider in the app that registers it."
		) from e
	for user in members:
		if not isinstance(user, str) or not user.strip():
			raise ProviderDataError(
				f"Membership provider {provider!r} returned {user!r} among the users matching "
				f"rule type {rule_type!r}. Every entry must be a non-empty user ID string. "
				f"Fix that provider in the app that registers it."
			)
	return members


def evaluate(provider: str, rule_type: str, config: dict) -> set[str]:
	p = _get_provider(provider)
	_get_rule_type(p, provider, rule_type)
	return _members_returned(p["evaluate"](rule_type, config or {}), provider, rule_type)


def validate_rule_config(provider: str, rule_type: str, config) -> None:
	if not provider or not rule_type:
		return  # mandatory check handles emptiness
	decl = _get_rule_type(_get_provider(provider), provider, rule_type)
	cfg = json.loads(config) if isinstance(config, str) else (config or {})
	for f in _applicable_fields(decl.get("fields", []) or [], cfg):
		if f.get("reqd") and not cfg.get(f["fieldname"]):
			frappe.throw(
				_("Rule field '{0}' is required for {1}").format(f.get("label", f["fieldname"]), rule_type)
			)


def _applicable_fields(fields: list, cfg: dict) -> list:
	"""The declared fields that apply to ``cfg`` — those with no ``depends_on``, and
	those whose named field holds the named value.

	The rule builder renders on the same rule, so a field it never drew must not be
	judged here: the row would be complete on screen and refused on save. The value
	compared is the one the control *shows* — a Select with nothing stored renders
	its declared default, so an untouched row is judged as the user sees it.
	"""
	shown = {}
	for f in fields:
		name = f.get("fieldname")
		if name:
			shown[name] = cfg.get(name) if cfg.get(name) is not None else f.get("default")

	applicable = []
	for f in fields:
		dep = f.get("depends_on")
		if dep and shown.get(dep.get("field")) != dep.get("value"):
			continue
		applicable.append(f)
	return applicable
