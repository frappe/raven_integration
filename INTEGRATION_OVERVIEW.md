# Raven Integration — What It Is and How It Works

A plain-language map of the `raven_integration` app: what it does, every path through it, and where its job starts and stops.

---

## In one sentence

**It keeps the right people in the right Raven chat channels automatically, based on rules — so nobody has to add or remove members by hand.**

Raven is the chat app (like Slack). This app is the "membership manager" sitting next to it.

---

## The problem it solves

Without this app, someone has to manually add every student to the right chat channel, and remove them when they leave. With hundreds of users and dozens of channels, that's slow and error-prone.

This app says: *"Define a rule once — e.g. 'everyone enrolled in Course X' — and I'll keep that channel's membership matching the rule, forever, on my own."*

---

## The core idea (the big picture)

```
  Some app (e.g. LMS)              Raven Integration                  Raven (chat)
  defines WHO matches a rule  -->  decides who SHOULD be in    -->    actually adds/
  ("enrolled in Course X")         each workspace & channel           removes members
```

Three players:

1. **A "provider"** — another app (like LMS) that knows how to answer *"which users match this rule?"*. The integration itself knows nothing about courses, enrollments, or any business logic. It just asks the provider.
2. **The integration (this app)** — holds the rules, figures out the expected member list, and applies the difference.
3. **Raven** — the actual chat system where members get added or removed.

This separation is the whole point: the integration is **generic**. Any Frappe app can plug in as a provider. LMS is intended to be the first one, but the app has no hard dependency on LMS.

---

## Key building blocks (the vocabulary)

| Thing | Plain meaning |
|-------|---------------|
| **Workspace** | A top-level container in Raven (like a Slack workspace). |
| **Channel** | A chat room inside a workspace. |
| **Workspace Mapping** | A record in this app that says "manage *this* Raven workspace." It holds no rules — its membership is derived from its channels. |
| **Channel Mapping** | "Keep *this* channel's members matching *these* rules." Rules live here and nowhere else. |
| **Membership Rule** | One condition, e.g. "matches LMS rule: enrolled in Course X". Each rule names a *provider*, a *rule type*, and some *config* (the opaque settings the provider understands). |
| **Provider** | The external app that answers "who matches this rule?". |
| **Condition tree** | How a channel's rules combine: a group joins its children with **or** (union) or **and** (intersection), and a child may be another group — so `A and (B or C)` is expressible. |
| **`added_by_rule` flag** | A hidden marker on each membership row. The app **only ever removes members it added itself** — see Safety below. |
| **Stale mapping** | A mapping whose Raven workspace/channel was deleted inside Raven. It keeps all its rules but stops syncing until an admin recreates the Raven record or deletes the mapping. |

---

## The data model (what gets stored)

- **Raven Workspace Mapping** — `workspace_label`, `workspace_type` (Public/Private), link to the real `Raven Workspace`, and a read-only `stale` flag. No conditions of its own, and no `enabled` switch: its membership is derived from its channels, so switching those off is what stops it syncing.
- **Raven Channel Mapping** — `channel_label`, `channel_type` (Public/Private/Open), link to its parent Workspace Mapping, link to the real `Raven Channel`, `enabled`, a read-only `stale` flag, and `member_rules_json` holding the **condition tree**.

A cleared `enabled` and a set `stale` both stop a mapping syncing, but they are not the same thing. `enabled` is the admin's switch — clearing it pauses the mapping on purpose, and only a channel mapping carries one. `stale` is the app recording a fact: the Raven record this mapping managed has been deleted. Admins set `enabled`; only the app sets and clears `stale`.
- **Raven Membership Settings** (single record) — one `enabled` switch that turns the whole integration on.

A rule is not a record of its own. It is a leaf of the condition tree inside `member_rules_json` — `label`, `provider`, `rule_type`, `status` (Active/Paused), `config` (JSON the provider interprets) — so it is saved, validated and deleted with the channel mapping that holds it.

---

## How membership is decided

For a **channel**, the app computes the expected member list:

1. Walk its condition tree, skipping **Paused** rules (they are absent, not "matches nobody").
2. Ask each rule's provider "who matches?" → get a set of users per rule.
3. Fold each group by its joiners: **or** = union, **and** = intersection. A group with nothing
   left in it drops out of its parent rather than emptying it.

Each joiner in `conjunctions` sits between the pair of conditions on either side of it, so a group
always has one fewer joiner than it has conditions. A group is usually wholly `or` or wholly `and`
— Frappe Learning's settings UI writes one operator per group — but the fold does not assume that:
a level mixing both operators folds with classic precedence, `and` binding tighter than `or`, so it
reads the way the expression looks rather than left to right:

```
conjunctions: [and, or, and]
conditions:   [A,   B,  C,  D]
=> (A and B) or (C and D)

conjunctions: [or, and]
conditions:   [A,  B,   C]
=> A or (B and C)        # not (A or B) and C
```

A mixed level can only reach this engine through the API — the settings UI never produces one.

For a **workspace**, membership is not computed from rules at all — it is **derived**:

```
workspace members = everyone who is in at least one channel of that workspace
```

Channels populate their workspace rather than carving it up. Adding someone to a channel puts them in the workspace; losing their last channel takes them back out. Nothing narrows a channel: a channel's rules are the whole statement of who belongs in it.

The derivation reads the channels' **actual** Raven membership, not their rules, so a channel the app is not syncing — disabled, stale, or never mapped at all — still holds its members in the workspace. This is why every sweep does channels first: by the time a workspace is reconciled, its channels already hold the membership their rules ask for.

Then the app compares expected vs. actual and applies the difference: add the missing people, remove the ones who no longer belong (but only ones it added itself).

---

## All the paths through the system

### 1. Setup / turning it on
- Admin checks both apps are installed (`is_setup`).
- Admin flips the `enabled` switch (`enable_integration`). **This is one-way — there is no "disable everything" button.** Turning it on kicks off a first full reconcile in the background.

### 2. Creating a managed workspace or channel
- Admin creates a **Workspace Mapping** → the app **creates a brand-new Raven Workspace** behind it (with a guaranteed-unique name).
- Admin creates a **Channel Mapping** under it → the app **creates a brand-new Raven Channel** in that workspace.
- The app never adopts an existing Raven workspace/channel on create — it makes its own. Adoption is a separate, explicit action (`link_workspace` / `link_channel`), and it refuses a direct message, a thread, and a channel that lives in another Raven workspace. The refusal is enforced on the mapping doctype rather than in the endpoint, so a direct write to the record is checked the same way, and the link is `set_only_once` so an adopted mapping cannot later be pointed somewhere else.
- Either way the app deletes no Raven record. It withdraws the membership its own rules granted and leaves the workspace and its channels standing.

### 3. Real-time sync (event-driven) — the fast path
- Every time *any* document is saved anywhere on the site, a lightweight handler checks: "is this a doctype a provider cares about?"
- If yes (e.g. an enrollment changed), it schedules a sync.
- **Debounced:** a burst of changes (like a bulk import of 500 enrollments) collapses into **one** sync per 30-second window, not 500 syncs.
- The sync re-checks every active mapping and corrects membership.

### 4. Nightly reconcile — the safety net
- Once a day, the app does a full sweep of every active mapping to catch anything the real-time path missed. Channels first, then workspaces — the second half is derived from the first.
- Before sweeping, it first marks **stale mappings** (see below), so a mapping whose Raven record has vanished is already excluded from the sweep in the same run.

### 5. Manual reconcile
- Admin can force a single workspace or channel to re-sync right now (`reconcile_now`), without waiting.

### 6. Previewing before saving
- `preview_rule` — "how many users would this one rule match?" (with a few sample names).
- `compute_rule_diff` — "if I save these new rules on this channel, how many get added/removed, and who gets removed?" So an admin sees the impact before committing. Channels only; a workspace has no rules to preview.

### 7. Deleting a mapping
- Delete a **Workspace Mapping** → its child Channel Mappings, and with them the rules, are deleted too.
- **The membership those rules granted is withdrawn**: every `added_by_rule` row on the workspace's channels and on the workspace itself is removed. Those people were in there on the authority of rules that are being deleted, and leaving them behind would leave a channel populated by a rule the site can no longer show, explain or undo.
- Members a human added inside Raven are **not** touched. They carry no `added_by_rule` flag, and the rule that this app only ever removes what it added holds on the way out exactly as it does during a sync.
- The **real Raven workspace/channel is left intact** — just no longer managed. Its history stays exactly as it was. The withdrawal itself is visible, though: Raven writes one "X was removed by Y." system message per membership row it deletes, and honours no flag that would suppress it.
- There is no option to do otherwise. **The app never deletes a Raven workspace or channel**, on any code path. "Delete" here only ever means "delete our own mapping record, the rules on it, and the membership those rules put in place".
- Deleting a single **Channel Mapping** withdraws the same way, narrowed to that one channel: its `added_by_rule` members go, the other channels are untouched, and the Raven channel itself survives.
- The workspace mapping survives a channel delete and stays managed, so its own membership stays **derived** rather than wiped: someone who was only in the workspace because of the deleted channel loses their workspace row too, and someone still in another channel keeps it. That is the same conclusion the next `sync_workspace_members` sweep would reach — doing it during the delete just means a paused or unswept workspace never sits on membership it can no longer account for.

### 8. Stale mappings (someone deleted the Raven side directly)
- An admin can delete a workspace/channel inside Raven directly. The app **allows** that — it deliberately does not block Raven's own delete.
- The mapping now points at a record that is gone. The nightly reconcile detects this and **marks the mapping `stale = 1`** and logs it. The mapping itself is *not* deleted, and the sweep touches nothing else — a channel mapping keeps whatever `enabled` the admin chose, because recreating the Raven record clears only `stale`.
- A stale mapping stops syncing: `sync_workspace_members` / `sync_channel_members` return `{"skipped": True, "reason": "raven_record_deleted"}` and do nothing else. No crashes, no orphan syncs.
- Everything the admin configured — the conditions, the label, the type — is still sitting there, intact.

### 9. Recovering a stale mapping
The UI surfaces a stale mapping with a **"Take action"** control offering exactly two choices:

- **Recreate the Raven record** (`recreate_workspace(name)` / `recreate_channel(name)`) — the app creates a brand-new Raven workspace/channel from the mapping's stored label, repoints the mapping's link at it, and clears `stale`. The rules survive and sync resumes on the next sweep. It returns the name of the new Raven record.
  The new workspace/channel starts empty: the conversation history of the deleted one is gone for good, and this app cannot recover it. What comes back is the rule configuration and the membership those rules produce.
- **Delete the mapping** (`delete_workspace(name)` / `delete_channel(name)`) — throw away our record and its rules. There is no Raven-side record left to delete, so nothing else happens.

Doing nothing is also fine. A stale mapping is inert; it just sits there not syncing.

---

## How another app plugs in (the provider contract)

An app becomes a provider by registering one function via a hook (`raven_membership_providers`). That function returns a description like:

```python
{
  "name": "LMS",                       # unique provider id
  "label": "Learning",                 # shown in the UI
  "rule_types": [                      # the kinds of rules it offers
    {"type": "course_enrollment",
     "label": "Enrolled in course",
     "fields": [...]}                  # what config the admin must fill in
  ],
  "evaluate": fn(rule_type, config) -> set of user emails,   # the actual "who matches?"
  "triggers": ["LMS Enrollment", ...]  # doctypes that should trigger a re-sync
}
```

That's the entire surface. The integration:
- never reads inside `config` (it's opaque — only the provider understands it),
- never imports the provider app,
- just calls `evaluate` to get a set of users.

This is what makes the app reusable across LMS, CRM, or anything else.

---

## Safety guarantees (why it won't wreck your data)

- **Never deletes a Raven workspace or channel.** Not when you delete a mapping, not when you uninstall the app, not through any endpoint. The only records it deletes are its own mappings — the rules ride along on the mapping, so they go with it — plus the individual member rows it added, including on the way out, when a workspace mapping is deleted.
- **Only removes what it added.** Every member the app adds is tagged `added_by_rule = 1`. Members added manually inside Raven are **never** touched. Removal only ever targets tagged rows.
- **One-way enable.** No global off-switch that would mass-remove everyone. You disable individual mappings instead.
- **Idempotent / race-safe.** Concurrent runs (nightly + event at once) can't double-add or crash — database uniqueness constraints catch the loser, and it's treated as already-done.
- **Fail-safe events.** The "on every save" handler can never break an unrelated document save; errors are logged, not raised.
- **Per-record error isolation.** If one mapping fails during a sweep, the others still complete; the failure is logged.
- **Admin-only.** Every management action requires a **manager role**, with strict input-type checks. **System Manager** always qualifies; a host app adds its own admin role through the `raven_integration_manager_roles` hook (LMS declares `Moderator`), and this app grants that role the permissions its endpoints need at install/migrate time. Nothing else in Raven is handed out with it — the app writes to Raven with its own authority, not the caller's.

---

## Scope — what it does and does NOT do

**It DOES:**
- Create Raven workspaces/channels it manages.
- Keep their membership matching rules, in real time and nightly.
- Let any app define the rules via the provider system.
- Give admins preview, diff, and manual-reconcile tools.
- Notice when a Raven workspace/channel it managed has been deleted, mark the mapping stale, and offer recreate-or-forget.

**It does NOT:**
- Delete Raven workspaces or channels. Ever. Deleting a mapping deletes only our own record and its rules.
- Know anything about courses, enrollments, or business logic — that's the provider's job.
- Send messages, manage chat content, or touch conversations.
- Remove members a human added manually.
- Sync membership *back* from Raven into the provider (it's one-directional: rules → Raven).
- Have its own management UI or client library inside this app. The management screens live in the consuming app's frontend (e.g. an LMS Settings page that calls this app's API), and so do the wire-format types and the mapping-list composable those screens use.

---

## Current status (as of this document)

- The `raven_integration` app is **fully built and standalone** — its own repo, 6 commits, full test suite, published to GitHub as `raizasafeel/raven_integration`.
- **No provider is registered in the current working tree.** The LMS provider (`lms/raven_provider.py` + hook + a Vue Settings UI repointed at this app's API) lives on a separate LMS branch (`feat/raven-integration`), not the branch checked out now.
- Requires the `raven` app to be installed (hard dependency).

So today it's a complete, generic engine waiting for a provider to be wired in on whichever app branch you check out.
