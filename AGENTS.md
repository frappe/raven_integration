# AGENTS.md — writing a Raven Integration provider

You are an automated coding agent. Someone pointed you here because they want **their Frappe app to
drive Raven workspace/channel membership** through the `raven_integration` app. This file is the
recipe. Follow it top to bottom and you will produce a working provider.

You are **not** editing `raven_integration` itself. You are adding a small amount of code to the
*consuming* app (call it `my_app`). This app stays domain-agnostic — it never learns what a "course"
or a "deal" is. It only ever asks your provider one question: *"which users match this rule?"*

If you need the exhaustive reference (every endpoint, every doctype field, the safety model), read
[`README.md`](README.md) and [`INTEGRATION_OVERVIEW.md`](INTEGRATION_OVERVIEW.md). This file is the
how-to; those are the spec.

---

## Mental model (read this before writing code)

- A **provider** is a dict your app declares. It has a `name`, a list of **rule types**, an
  **`evaluate`** function, and (optionally) a list of **triggers**.
- A **rule type** is a kind of question the admin can ask — e.g. "Enrolled in course". Each rule type
  declares its own **config fields** (the inputs the admin fills in).
- A **rule** is one saved instance of a rule type: `rule_type` + a `config` dict (`{"course": "..."}`)
  + a `status` (Active/Paused). Rules live on a mapping (a managed workspace or channel).
- `raven_integration` **combines** the active rules on a mapping into an expected member set and makes
  Raven match it. You never call Raven. You never touch membership. You only answer
  `evaluate(rule_type, config) -> set of user names`.

Everything below is about writing those four things correctly.

---

## Step 1 — declare the hook

In the consuming app's `hooks.py`:

```python
# my_app/hooks.py
raven_membership_providers = ["my_app.raven_provider.get_provider"]
```

The value is a list of **dotted paths to zero-argument callables**. Each callable returns one provider
dict. Most apps register exactly one.

Do **not** add `raven_integration` to `required_apps`. The integration is optional — your app must
import and run even when it is not installed (see Step 5). It is `raven_integration` that depends on
`raven`, not your app that depends on `raven_integration`.

If your app has its own admin role and you want those admins managing the integration — not just
System Managers — declare it in the same `hooks.py`:

```python
# my_app/hooks.py
raven_integration_manager_roles = ["Moderator"]
```

`raven_integration` grants that role the DocPerms its endpoints need on install/migrate, and revokes
them on uninstall. This is a separate hook from the provider one on purpose: registering rule types
should not hand out management rights. Declare it only for a role you are happy to give full control
of every mapping — there is no per-mapping scoping. Note that if your app's UI already shows the
Raven settings screen to that role, this hook is not optional; without it every call the screen makes
answers `PermissionError`.

## Step 2 — write the provider callable

Create `my_app/raven_provider.py`:

```python
from __future__ import annotations

import frappe


def get_provider() -> dict:
	return {
		"name": "MY_APP",             # REQUIRED. Unique id, stored on every rule row this provider owns.
		"label": "My App",           # optional; shown in the admin UI. Falls back to name.
		"rule_types": RULE_TYPES,     # REQUIRED. See Step 3.
		"evaluate": evaluate,         # REQUIRED. A callable(rule_type, config) -> iterable of User names.
		"triggers": TRIGGERS,         # optional. See Step 6.
	}
```

The registry validates this dict the moment any app is (un)installed or migrated. It **throws with a
named, actionable error** if:

- the callable returns something other than a `dict`;
- `name`, `evaluate`, or `rule_types` is missing;
- `name` is not a non-empty string;
- `evaluate` is not callable;
- `rule_types` is not a list.

`name` is the primary key of the provider registry. If two installed apps register the same `name`,
installation throws a duplicate-provider error — pick a name unlikely to collide (your app name in
caps is a good default).

## Step 3 — declare rule types and their config fields

`rule_types` is a list. Each entry describes one kind of rule the admin can create.

```python
RULE_TYPES = [
	{
		"type": "course_enrollment",       # REQUIRED. Stored on the rule row as `rule_type`.
		"label": "Enrolled in course",     # human-readable "matches" text in the rule builder.
		"fields": [                        # the config schema for THIS rule type.
			{
				"fieldname": "course",     # key that will appear in the config dict.
				"label": "Course",         # field label in the UI.
				"fieldtype": "Link",       # how the UI renders the input.
				"options": "LMS Course",   # fieldtype-specific (link target, select options, …).
				"reqd": 1,                 # the core enforces this on save.
			},
			{
				"fieldname": "payment_filter",
				"label": "Payment",
				"fieldtype": "Select",
				"options": ["Any", "Paid", "Free"],
				"default": "Any",
			},
		],
	},
]
```

What the **core actually reads** from a field: only `fieldname`, `reqd`, and `label`. When a rule is
saved, `validate_rule_config` throws if any field with `reqd: 1` is missing or empty in the config.

Every other key — `fieldtype`, `options`, `default`, `description` — is **passed through untouched**
to the consuming app's form renderer. The rule-builder UI lives in the consuming app, not here, so
these keys mean whatever your UI wants them to mean. Common `fieldtype`s in existing providers:
`Link`, `MultiSelect` (with `options` = a doctype name), `Select` (with `options` = a list of
strings), `Check`, `Data`.

`type` is validated on every save and every evaluation: evaluating a rule whose `rule_type` your
provider does not declare throws **before** your `evaluate` is called. So `evaluate` only ever
receives a `rule_type` you listed here.

## Step 4 — write `evaluate`

This is the whole job. Given a rule type and its config, return the set of **`User` names** (the
`User.name`, i.e. the login email/id) that the rule matches *right now*.

```python
def evaluate(rule_type: str, config: dict) -> set[str]:
	"""Return the User names that match this rule. The core wraps the result in set()."""
	if rule_type == "course_enrollment":
		filters = {"course": config["course"]}
		if config.get("payment_filter") == "Paid":
			filters["payment"] = ["is", "set"]
		return set(frappe.get_all("LMS Enrollment", filters=filters, pluck="member"))
	# unreachable if RULE_TYPES is correct, but fail loud rather than returning [] silently:
	raise ProviderDataError(f"Unknown rule_type: {rule_type!r}")
```

Rules for a correct `evaluate`:

- **Return an iterable of `User` names.** A `set`, `list`, or generator all work; the registry calls
  `set(...)` on whatever you return. Return an **empty** set for "matches nobody" — that is a valid,
  meaningful answer (it evicts everyone the rule previously added).
- **It must be authoritative and current.** Membership sync trusts you completely: whoever you return
  is added, whoever you omit is removed. There is no "partial" answer. If a data source can miss
  people (e.g. mirrored records), union in the other source rather than under-reporting.
- **`config` is opaque to the core.** It is exactly the dict the admin saved. Coerce defensively —
  a `MultiSelect` may arrive as a JSON string, a list of strings, or a list of `{"course": name}`
  dicts depending on UI version. Do not assume a shape you did not write.
- **Never fall back to "everyone" on an empty scope.** A scoped rule whose scope resolves to nothing
  must return the empty set, not an unfiltered `frappe.get_all`. Returning the whole site because a
  filter was empty is the classic membership-sync disaster.
- **Query efficiently.** `evaluate` runs on every real-time resync and every nightly sweep, once per
  rule. Prefer `frappe.get_all(..., pluck=..., distinct=True)` and `frappe.qb`; avoid loading
  documents in a loop. (This app's own convention forbids raw SQL — use the query builder.)
- **No side effects.** `evaluate` is a pure read. Do not write, enqueue, or mutate anything.

### Error handling

When a rule's config points at something that no longer exists (a deleted course, a renamed batch),
raise:

```python
from raven_integration.exceptions import ProviderDataError
```

The engine treats `ProviderDataError` (and `frappe.ValidationError`) specially:

- **Read/preview paths** (`preview_rule`, UI member counts) run non-strict: the bad rule is logged to
  the Error Log and **skipped**, so one broken rule does not blank out a whole preview.
- **Sync paths** run strict: the error propagates and that mapping's sync fails loudly rather than
  silently evicting everyone. The rest of the sweep still runs — one mapping's failure never aborts
  the others.

Returning `set()` for a broken config would be **wrong** — it reads as "matches nobody" and evicts
real members. Raise instead.

## Step 5 — keep the dependency optional

`raven_integration` is an optional out-of-tree app. Importing your provider module must not hard-fail
when it is absent. Guard the one import that reaches into it:

```python
try:
	from raven_integration.exceptions import ProviderDataError
except ImportError:
	# Mirror the class so `except ProviderDataError` on the raven_integration side still
	# catches what we raise when the app *is* installed (the real class then wins).
	class ProviderDataError(Exception):
		pass
```

`get_provider` itself is only ever called *by* `raven_integration`, so it is safe — if the app is not
installed, nobody calls it. Only top-level imports need the guard.

---

## How rules behave once you've shipped the provider

You write the provider; the admin writes the **rules** through the consuming app's UI. You should
understand the semantics so your rule types compose sensibly.

### Conditions — a tree of and / or

A channel holds a **condition tree** in `member_rules_json`, not a flat list. A group joins its
children with `and` or `or`; a child is either a rule or another group:

```json
{"conjunctions": ["and"],
 "conditions": [
   {"provider": "lms", "rule_type": "enrolled_in_course", "status": "Active", "config": {}},
   {"conjunctions": ["or"], "conditions": [ ... ]}
 ]}
```

- **`or`** → the **union** of its children's populations.
- **`and`** → the **intersection**. A user must match every child.

`conjunctions[i]` joins `conditions[i]` to `conditions[i + 1]`, so a group always holds exactly one
fewer conjunction than it has conditions. Frappe Learning's settings UI writes one conjunction per
group, so a group coming through that UI is wholly `and` or wholly `or` — but a mixed level can
still arrive through this app's API (a provider composing the tree programmatically, say), and the
fold handles it with classic precedence: `and` binds tighter than `or`, so it reads the way the
expression looks, not left to right.

```
conjunctions: [and, or, and]
conditions:   [A,   B,  C,  D]
=> (A and B) or (C and D)

conjunctions: [or, and]
conditions:   [A,  B,   C]
=> A or (B and C)        # not (A or B) and C
```

So "Enrolled in course X" **or** "Is staff" is two rules under `or`, and
"Enrolled in course X **and** (Is staff **or** Is an evaluator)" is a nested group. Design rule
types as independent predicates and let the admin compose them — don't build one mega rule type
with every filter baked in.

A **Paused** rule is absent from its group rather than evaluating to nobody, and the people it
already added are frozen rather than evicted. That only reads as "adds nobody new" while the rule
*adds* people, so pausing is offered only where every group above the rule joins with `or`.

### Channels narrow their workspace

```
workspace members = combine(workspace's active rules)
channel members   = combine(channel's active rules)  ∩  workspace members
```

A channel can only ever **narrow** its parent workspace — a channel rule cannot pull in someone who
isn't in the workspace. The single exception: a workspace with **no active rules** expresses no
opinion, so it imposes no constraint and the channel's own rules stand alone.

### Paused rules and the "no active rules" rule

- A rule with `status: "Paused"` (the UI calls it **Disabled**) does not participate in the union or
  intersection.
- A mapping with **zero active rules** is treated as **unmanaged**, not empty. Sync skips it
  (`reason: "no_active_rules"`) instead of evicting everyone. This is deliberate: deleting or pausing
  the *last* rule should not nuke the channel.
- Under **OR only**, a disabled rule additionally **freezes** the members it had already added
  (`current ∩ that rule's population`) rather than evicting them. Disable is not offered under AND,
  because there a rule *narrows* the set and dropping it would *add* people. You don't implement any
  of this — the engine does — but your `evaluate` must still return correct populations for a paused
  rule, because the freeze path evaluates it.

### Members the integration added vs. members humans added

The integration only ever **removes users it added itself** (rows flagged `added_by_rule = 1`).
Someone an admin added by hand inside Raven is never touched by your rules. You don't manage this —
just know that "removed because no longer matches" only applies to rule-added members.

---

## Step 6 — triggers (real-time resync)

By default your rules re-sync nightly. To make membership update **in real time** when your source
data changes, list the doctypes whose saves should schedule a resync:

```python
TRIGGERS = ["LMS Enrollment", "LMS Batch Enrollment", "Course Instructor"]
# dict form is also accepted: [{"doctype": "LMS Enrollment"}, ...]
```

A save (`after_insert` / `on_update` / `on_trash`) on **any** of these, anywhere on the site,
schedules a resync. Bursts are debounced into one sweep per ~30-second window, so a 500-row import
causes one sync, not 500.

List every doctype your `evaluate` reads from. If "enrolled in course" depends on `LMS Enrollment`
and `LMS Payment`, trigger on both — otherwise a payment change won't re-sync until the nightly
sweep.

The union of all providers' triggers is cached in Redis and invalidated automatically on
install/migrate/app-install/app-uninstall, with a one-hour TTL. If you edit `TRIGGERS` on a running
site and don't want to wait out the TTL:

```python
frappe.call("raven_integration.events.clear_trigger_doctypes_cache")
```

## Step 7 (optional) — react to applied membership changes

To observe what sync actually did (audit log, welcome message, etc.), register the
`after_raven_member_synced` hook. It fires **once per member row** added or removed, on both the
channel and the workspace surface:

```python
# my_app/hooks.py
after_raven_member_synced = ["my_app.handlers.on_raven_member_synced"]
```

```python
# my_app/handlers.py
def on_raven_member_synced(*, scope, target, user, action, source, **kwargs):
	# scope:  "channel" | "workspace"
	# target: the Raven Channel / Raven Workspace name
	# user:   the Frappe User the row is for
	# action: "added" | "removed"
	# source: "event" (debounced, save-triggered) | "reconcile" (nightly sweep)
	...
```

Keyword arguments only. Accept `**kwargs` so a future keyword doesn't break you. Handlers run in
`installed_apps` order, are **not** transactional (the row is already written), and every exception
is logged and swallowed — a bad handler can never abort a sweep. Do not do heavy or blocking work
here; enqueue it.

---

## Step 8 — test your provider

Write your tests against a real site with `raven_integration` installed. You do **not** need to stand
up channels to test `evaluate` — it is a pure function:

```python
import frappe
from my_app.raven_provider import evaluate


class TestMyAppProvider(frappe.tests.UnitTestCase):
	def test_course_enrollment_matches_enrolled_users(self):
		# arrange: create a course + an enrollment
		# act:
		members = evaluate("course_enrollment", {"course": "COURSE-1"})
		# assert:
		self.assertIn("student@example.com", members)

	def test_empty_scope_matches_nobody_not_everybody(self):
		self.assertEqual(evaluate("course_enrollment", {"course": "does-not-exist"}), set())
```

Run:

```bash
bench --site $YOUR_SITE run-tests --module my_app.tests.test_raven_provider
```

`raven_integration`'s own suite proves the engine/registry with a **domain-free fake provider**
(`raven_integration/tests/fake_provider.py`) — read it for the minimal shape of a valid provider.

---

## Pre-flight checklist

Before you report the integration done, verify every box:

- [ ] `raven_membership_providers` in `hooks.py` points at a real zero-argument callable.
- [ ] If your app surfaces the Raven settings UI to a non-System-Manager role, that role is declared in
      `raven_integration_manager_roles`.
- [ ] `get_provider()` returns a dict with `name`, `rule_types`, `evaluate` (and `label`/`triggers` if
      wanted). `name` is a non-empty string unlikely to collide.
- [ ] Every `rule_types[].type` your UI can create is handled in `evaluate`.
- [ ] Every field with `reqd: 1` is one your `evaluate` actually requires.
- [ ] `evaluate` returns `User` names, is authoritative/current, and returns `set()` (never "everyone")
      for an empty scope.
- [ ] `evaluate` raises `ProviderDataError` — not returns `set()` — when config references something
      deleted.
- [ ] `evaluate` is a pure read: no writes, no raw SQL (use `frappe.qb` / `frappe.get_all`).
- [ ] The `raven_integration.exceptions` import is guarded so your app still imports without it.
- [ ] `TRIGGERS` covers every doctype `evaluate` reads, or you accept nightly-only sync.
- [ ] Tests cover: a matching case, an empty-scope case, and a deleted-reference (raises) case.

## Common mistakes (do not do these)

| Mistake | Why it's wrong |
|---|---|
| Returning `[]`/`set()` when config is broken | Reads as "matches nobody" → evicts real members. Raise `ProviderDataError`. |
| `frappe.get_all(dt)` with no filter when a scope is empty | Returns the whole site → mass-adds everyone. Return `set()`. |
| Adding `raven_integration` to `required_apps` | Makes the integration mandatory; it must be optional. |
| Un-guarded `from raven_integration...` import at module top | Breaks your app on sites without the integration. |
| Baking OR/AND into one rule type | Let the admin compose independent rule types in the condition tree. |
| Writing/enqueuing inside `evaluate` | It runs on every sweep and must stay a pure read. |
| Raw SQL in the provider | Use `frappe.qb` / `frappe.get_all` — this project forbids raw SQL. |

## A complete minimal provider

```python
# my_app/raven_provider.py
from __future__ import annotations

import frappe

try:
	from raven_integration.exceptions import ProviderDataError
except ImportError:
	class ProviderDataError(Exception):
		pass


RULE_TYPES = [
	{
		"type": "course_enrollment",
		"label": "Enrolled in course",
		"fields": [
			{"fieldname": "course", "label": "Course", "fieldtype": "Link",
			 "options": "LMS Course", "reqd": 1},
		],
	},
]

TRIGGERS = ["LMS Enrollment"]


def get_provider() -> dict:
	return {
		"name": "MY_APP",
		"label": "My App",
		"rule_types": RULE_TYPES,
		"evaluate": evaluate,
		"triggers": TRIGGERS,
	}


def evaluate(rule_type: str, config: dict) -> set[str]:
	if rule_type == "course_enrollment":
		course = config.get("course")
		if not course or not frappe.db.exists("LMS Course", course):
			raise ProviderDataError(f"Course {course!r} does not exist")
		return set(frappe.get_all("LMS Enrollment", filters={"course": course}, pluck="member"))
	raise ProviderDataError(f"Unknown rule_type: {rule_type!r}")
```

```python
# my_app/hooks.py
raven_membership_providers = ["my_app.raven_provider.get_provider"]
```

That's a working provider. Everything else — creating the workspace/channel mappings, writing rules
against your rule types, combining them, and applying the diff to Raven — is the admin's job through
the UI and `raven_integration`'s job at runtime. You supplied the one thing only your app can answer:
who matches.

For the real, battle-tested version of this file, read `lms/raven_provider.py` in Frappe Learning —
it implements four rule types (enrolled students, students of courses, students of batches, staff by
role) with payment filters and course/batch scoping, and is the reference every point above is drawn
from.
