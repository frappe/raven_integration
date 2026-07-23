# Raven Integration

Keep [Raven](https://github.com/The-Commit-Company/raven) workspace and channel membership in sync
with rules supplied by any Frappe app.

Define a rule once — "everyone enrolled in Course X" — and this app keeps the matching Raven
workspace and channel membership correct: in real time when the source data changes, and again on a
nightly reconcile.

The app is deliberately **domain-agnostic**. It knows nothing about courses, enrollments, deals or
tickets. It asks a *provider* — another installed app that registers itself through the
`raven_membership_providers` hook — "which users match this rule?", and applies the answer to Raven.

## What it does

- Creates the Raven workspace/channel behind each mapping record.
- Computes the expected member set from rules, combined with `Any (OR)` or `All (AND)`.
- Applies the diff against Raven: adds who's missing, removes who no longer matches.
- Reacts to source-data changes in real time (debounced), plus a nightly full sweep.
- Exposes preview/diff endpoints so an admin can see the impact before saving rules.

## What it does not do

- **No destructive writes against Raven.** The app never deletes a Raven workspace or channel —
  not on mapping delete, not on uninstall, not on any endpoint. The only records it deletes are its
  own mappings and rules, and the member rows it added itself.
- No business logic of its own — that lives in the provider.
- No messaging, no chat content, no conversation management.
- No management UI, and no client library. This app is backend + whitelisted API; the screens
  belong in the consuming app, and so do the wire-format types, the mapping-list composable and
  any provider-specific rule vocabulary.
- No reverse *membership* sync. Membership flows rules → Raven, never back — a member added or
  removed inside Raven is not written back into a rule. (A workspace's name and visibility *are*
  mirrored both ways: change them on the mapping or inside Raven and the other side follows.)

## Safety model

- **It only removes members it added.** Every row the app inserts is flagged with the custom field
  `added_by_rule = 1` (added to `Raven Channel Member` and `Raven Workspace Member` on install and
  on every migrate). Removal only ever targets flagged rows, so members a human added inside Raven
  are never touched.
- **It never deletes a Raven workspace or channel.** Deleting a mapping deletes the mapping and its
  rules — the Raven workspace/channel it was managing survives, along with its history and everyone
  in it. No endpoint in this app can destroy a Raven workspace or channel.
- **Enabling is one-way.** There is an `enable_integration` endpoint and no global disable, so no
  single click can mass-evict members. Disable individual mappings instead (clear `enabled`).
- **A mapping with no active rules is unmanaged, not empty.** Deleting or pausing the last rule
  makes sync skip the mapping (`reason: "no_active_rules"`) rather than evicting everyone.
- **Errors are isolated.** The wildcard document handler never raises into an unrelated save, and a
  failure on one mapping during a sweep does not abort the rest — both log instead.
- **Admin-only.** Every management endpoint calls `frappe.only_for(["System Manager"])` and
  type-checks its inputs.

## Requirements & installation

- The `raven` app must be installed on the site. It is declared in `required_apps`, so bench will
  refuse to install this app without it.
- Python >= 3.10 (in practice, whatever your Frappe version requires).

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/raizasafeel/raven_integration
bench --site $YOUR_SITE install-app raven_integration
```

Installing adds the `added_by_rule` custom field (and its index) to Raven's member doctypes.
Uninstalling removes it.

Membership sync stays **off** until it is enabled — `enabled` on the `Raven Membership Settings`
single. Enabling also queues an initial full reconcile:

```python
frappe.call("raven_integration.api.enable_integration")
```

## Doctypes

| Doctype | Kind | Purpose |
|---|---|---|
| **Raven Workspace Mapping** | Document | A managed Raven workspace. `workspace_label`, `workspace_type` (Public/Private), `rule_combinator`, `enabled`, `stale` (read-only), `member_rules` (child table), and a read-only `raven_workspace` link to the Raven workspace it created. Named `RWM-{workspace_label}`. |
| **Raven Channel Mapping** | Document | A managed channel inside a mapped workspace. `channel_label`, `workspace` (link to the workspace mapping), `channel_type` (Public/Private/Open), `rule_combinator`, `enabled`, `stale` (read-only), `member_rules`, and a read-only `raven_channel` link. Named `RCM-{channel_label}`. |
| **Raven Membership Rule** | Child table | One rule row: `label`, `provider`, `rule_type`, `status` (Active/Paused), and `config` (JSON, opaque to this app — only the provider interprets it). |
| **Raven Membership Settings** | Single | One `enabled` switch that gates the whole integration. |

All three non-child doctypes are System Manager-only.

Two flags stop a mapping from syncing, and they mean different things. A cleared `enabled` is an admin's
choice — pause this mapping. `stale` is a fact the app discovered — the Raven workspace/channel this
mapping pointed at no longer exists (see [Stale mappings](#stale-mappings)). `stale` is read-only:
it is set and cleared by the app, never by hand.

Creating a mapping **creates a new** Raven workspace/channel behind it (with a uniqueness-safe name);
it never adopts an existing one. Deleting a workspace mapping cascades to its channel mappings.
Deletion stops there: the app deletes only its own mappings and their rules, and the Raven workspace
or channel is left intact — unmanaged, but whole.

## Stale mappings

Nothing stops an admin from deleting a workspace or channel inside Raven itself. The app does not
block that delete (its mappings are listed in `ignore_links_on_delete`), so the mapping is left
pointing at a record that is gone.

When that happens the mapping is **not** deleted. It is marked `stale = 1`:

- The nightly reconcile checks every mapping's Raven link before sweeping and flags the ones whose
  target has vanished.
- A stale mapping stops syncing. `sync_workspace_members` / `sync_channel_members` return
  `{"skipped": True, "reason": "raven_record_deleted"}` and touch nothing.
- The mapping's rules, labels and settings are all still there, untouched.

Two ways out, both offered as a "Take action" control in the consuming UI:

| Action | Endpoint | Effect |
|---|---|---|
| **Recreate** | `recreate_workspace(name)` / `recreate_channel(name)` | Creates a fresh Raven workspace/channel from the mapping's stored label, repoints the `raven_workspace` / `raven_channel` link at it, and clears `stale`. The configured rules survive and resume syncing. Returns the new Raven record's name. |
| **Delete the mapping** | `delete_workspace(name)` / `delete_channel(name)` | Removes the mapping and its rules. There is no Raven-side record left to delete. |

Recreating gives you a new, empty Raven workspace/channel — the messages and history that lived in
the deleted one are gone, and this app cannot bring them back. What it restores is the rule
configuration and the membership those rules imply.

## How membership is computed

For a workspace mapping:

```
members = combine(evaluate(rule) for rule in active rules)     # OR → union, AND → intersection
```

For a channel mapping:

```
members = combine(its own active rules) ∩ (its workspace's members)
```

A channel can only ever narrow its parent workspace — a channel rule cannot pull someone into the
workspace who does not belong there. The one exception: if the parent workspace has **no active
rules** it expresses no opinion about membership, so it imposes no constraint and the channel's own
rules stand alone.

Paused rules never participate. Zero active rules yields an empty set, which sync treats as
"unmanaged" (see Safety model).

## The provider contract

An app becomes a provider by pointing `raven_membership_providers` at a zero-argument callable that
returns a provider dict:

```python
# my_app/hooks.py
raven_membership_providers = ["my_app.raven_provider.get_provider"]
```

```python
# my_app/raven_provider.py
import frappe


def get_provider() -> dict:
	return {
		"name": "MY_APP",                       # unique provider id, stored on each rule row
		"label": "My App",                      # shown in the consuming UI
		"rule_types": [
			{
				"type": "course_enrollment",    # stored on the rule row as rule_type
				"label": "Enrolled in course",  # used as the rule's human-readable "matches" text
				"fields": [                     # config schema, rendered by the consuming UI
					{
						"fieldname": "course",
						"label": "Course",
						"fieldtype": "Link",
						"options": "LMS Course",
						"reqd": 1,
					}
				],
			}
		],
		"evaluate": evaluate,
		"triggers": ["LMS Enrollment"],         # doctypes whose saves should trigger a resync
	}


def evaluate(rule_type: str, config: dict) -> set[str]:
	"""Return the set of User names matching this rule."""
	if rule_type == "course_enrollment":
		return set(
			frappe.get_all(
				"LMS Enrollment", filters={"course": config["course"]}, pluck="member"
			)
		)
	return set()
```

### Contract details

- **`name`** keys the provider registry. Two providers with the same `name` collide — last one loaded
  wins.
- **`label`** is optional; it falls back to `name`.
- **`rule_types[].type`** is validated on every rule save and on every evaluation. Evaluating a rule
  whose `rule_type` is not declared here throws before `evaluate` is ever called.
- **`rule_types[].fields`** — the core only reads `fieldname`, `reqd` and `label` from each entry, to
  enforce required config keys when a rule is saved. Every other key (`fieldtype`, `options`, …) is
  passed through untouched for the consuming app's form renderer.
- **`evaluate(rule_type, config)`** returns any iterable of `User` names; the registry wraps it in a
  `set()`. `config` is whatever the admin saved — the core never looks inside it. Raise
  `raven_integration.exceptions.ProviderDataError` (or `frappe.ValidationError`) when the config
  references something that no longer exists; read paths log and skip such a rule, sync paths fail
  loudly.
- **`triggers`** is a list of doctype names. Dict entries of the form `{"doctype": "..."}` are also
  accepted. A save on any of these doctypes anywhere on the site schedules a resync.

### Trigger caching

The union of all providers' `triggers` is cached in Redis under
`raven_integration:trigger_doctypes`, because the wildcard document handler consults it on every save
site-wide. A provider is declared in another app's `hooks.py`, so the cache is invalidated
automatically on `after_install`, `after_migrate`, `after_app_install` and `after_app_uninstall` —
installing or removing a provider app is picked up without any manual step. The entry also carries a
one-hour TTL, so anything that bypasses those hooks self-heals.

If you edit a provider's `triggers` in place on a running site and do not want to wait out the TTL:

```python
frappe.call("raven_integration.events.clear_trigger_doctypes_cache")
```

### Reacting to membership changes

An app can observe applied changes through the `after_raven_member_synced` hook, declared (empty) in
this app's `hooks.py`. It fires once per member row added or removed, on both the channel and the
workspace surface.

```python
# my_app/hooks.py
after_raven_member_synced = ["my_app.handlers.on_raven_member_synced"]
```

```python
# my_app/handlers.py
def on_raven_member_synced(*, scope, target, user, action, source, **kwargs):
	# scope:  "channel" | "workspace"
	# target: the Raven Channel name (scope="channel") or Raven Workspace name
	# user:   the Frappe User the row is for
	# action: "added" | "removed"
	# source: "event" (debounced, save-triggered) | "reconcile" (nightly sweep)
	...
```

Handlers take keyword arguments only and run in `installed_apps` order. They are not transactional —
the membership row is already written when the handler runs. Every exception a handler raises is
logged to the Error Log and swallowed, so one bad handler can never abort a sweep or block the
handlers after it. Accept `**kwargs` so a future keyword does not break your handler.

## When sync runs

1. **Real time.** A wildcard `doc_events` handler sees every save on the site, cheaply checks the
   cached trigger-doctype set, and schedules a resync. Bursts are debounced into one sweep per
   30-second window, so a bulk import of 500 records causes one sync, not 500.
2. **Nightly.** `raven_integration.scheduler.reconcile_all` runs daily. It first marks mappings
   `stale` whose Raven workspace/channel was deleted directly inside Raven, then runs the full
   sweep — so a newly stale mapping is already excluded from the sweep in the same run.
3. **Manually.** `reconcile_now(target_doctype, name)` forces one mapping to re-sync immediately.

## API

All endpoints live in `raven_integration.api`. Every one except `is_setup` requires the System
Manager role.

| Endpoint | Purpose |
|---|---|
| `is_setup` | Whether Raven and this app are installed, and whether sync is enabled. |
| `enable_integration` | Turn sync on and queue the first full reconcile. |
| `list_providers` | Registered providers and their rule types, for the rule-builder UI. |
| `list_workspaces` / `get_workspace` | List and detail (incl. member count, `stale`, and rule rows). |
| `create_workspace` / `update_workspace` / `delete_workspace` | CRUD. `delete_workspace` removes the mapping and its child channel mappings; the Raven workspace is never deleted. |
| `recreate_workspace` | For a stale mapping: create a fresh Raven workspace from the stored label, repoint the link, clear `stale`. Returns the new Raven workspace name. |
| `set_workspace_enabled` / `set_workspace_type` / `set_workspace_label` / `set_workspace_combinator` | Inline edits. Enabling re-syncs in the background. |
| `list_channels` / `get_channel` | List and detail, incl. `stale`. |
| `create_channel` / `update_channel` / `delete_channel` | CRUD. `delete_channel` removes the mapping only; the Raven channel is never deleted. |
| `recreate_channel` | For a stale mapping: create a fresh Raven channel from the stored label, repoint the link, clear `stale`. Returns the new Raven channel name. |
| `set_channel_enabled` / `set_channel_type` / `set_channel_label` / `set_channel_combinator` | Inline edits. |
| `reconcile_now` | Force one mapping to re-sync now. |
| `preview_rule` | How many users a single (unsaved) rule matches, plus 5 sample names. |
| `compute_rule_diff` | How many members a proposed rule set would add/remove, plus up to 10 sample names of those removed. |

`create_workspace` and `create_channel` auto-name (`Workspace N` / `Channel N`) when no label is
given, so a UI can add a row in one click.

## Internals

[`INTEGRATION_OVERVIEW.md`](INTEGRATION_OVERVIEW.md) is the long-form walkthrough: every path through
the system, the data model, the safety guarantees and the scope boundaries, in plain language.

Module map:

| Module | Responsibility |
|---|---|
| `registry.py` | Loads providers from the hook; validates rule config; dispatches `evaluate`. |
| `engine.py` | Combines rule populations (OR/AND); workspace ∩ channel logic; duplicate-rule validation. |
| `sync_service.py` | Applies the diff to Raven; creates workspaces/channels; `added_by_rule` bookkeeping. |
| `events.py` | Wildcard doc event handler, debounce, shared resync sweep. |
| `scheduler.py` | Nightly reconcile and stale-mapping detection. |
| `api.py` | Whitelisted management endpoints. |
| `custom_fields.py` | Installs/removes the `added_by_rule` custom field and its index. |

## Tests

```bash
bench --site $YOUR_SITE run-tests --app raven_integration
```

Tests use a domain-free fake provider (`raven_integration/tests/fake_provider.py`) so nothing in the
suite depends on LMS or any other consumer app.

## Contributing

This app uses `pre-commit` for formatting and linting. [Install pre-commit](https://pre-commit.com/#installation)
and enable it:

```bash
cd apps/raven_integration
pre-commit install
```

Configured tools: ruff, eslint, prettier, pyupgrade.

## License

MIT
