# A second deterministic provider, so multi-provider behaviour is exercised —
# different name, different rule types, different populations, no domain coupling.
# Its populations deliberately overlap FAKE's on b@example.com, so a mapping mixing
# both providers has a non-trivial union and intersection.
_USERS = {
	"only-b": {"b@example.com"},
	"b-and-c": {"b@example.com", "c@example.com"},
}


def get_provider():
	return {
		"name": "FAKE2",
		"label": "Fake Two",
		"rule_types": [
			{"type": "only-b", "label": "Only B", "fields": []},
			{"type": "b-and-c", "label": "B and C", "fields": []},
		],
		"evaluate": lambda rule_type, config: set(_USERS.get(rule_type, set())),
		# "Another Doctype" is shared with FAKE so trigger_doctypes() has something to union.
		"triggers": ["Another Doctype", "Third Doctype"],
	}
