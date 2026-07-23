# A deterministic provider for engine/registry/sync tests — no domain coupling.
_USERS = {
	("FAKE", "always-a"): {"a@example.com"},
	("FAKE", "always-ab"): {"a@example.com", "b@example.com"},
}


def get_provider():
	return {
		"name": "FAKE",
		"label": "Fake",
		"rule_types": [
			{"type": "always-a", "label": "Always A", "fields": []},
			{"type": "always-ab", "label": "Always AB", "fields": []},
		],
		"evaluate": lambda rule_type, config: set(_USERS.get(("FAKE", rule_type), set())),
		"triggers": ["Some Doctype", "Another Doctype"],
	}
