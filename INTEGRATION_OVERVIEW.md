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
| **Workspace Mapping** | A record in this app that says "manage *this* Raven workspace's members using *these* rules." |
| **Channel Mapping** | Same, but for a single channel. |
| **Membership Rule** | One condition, e.g. "matches LMS rule: enrolled in Course X". Each rule names a *provider*, a *rule type*, and some *config* (the opaque settings the provider understands). |
| **Provider** | The external app that answers "who matches this rule?". |
| **Rule combinator** | How multiple rules on one record combine: **Any (OR)** = match any rule, **All (AND)** = must match every rule. |
| **`added_by_rule` flag** | A hidden marker on each membership row. The app **only ever removes members it added itself** — see Safety below. |
| **Stale mapping** | A mapping whose Raven workspace/channel was deleted inside Raven. It keeps all its rules but stops syncing until an admin recreates the Raven record or deletes the mapping. |

---

## The data model (what gets stored)

- **Raven Workspace Mapping** — `workspace_label`, `workspace_type` (Public/Private), link to the real `Raven Workspace`, an `enabled` switch, a read-only `stale` flag, a `rule_combinator`, and a child table of **member rules**.
- **Raven Channel Mapping** — `channel_label`, `channel_type` (Public/Private/Open), link to its parent Workspace Mapping, link to the real `Raven Channel`, `enabled`, a read-only `stale` flag, `rule_combinator`, and its own child table of **member rules**.

A cleared `enabled` and a set `stale` both stop a mapping syncing, but they are not the same thing. `enabled` is the admin's switch — clearing it pauses the mapping on purpose. `stale` is the app recording a fact: the Raven record this mapping managed has been deleted. Admins set `enabled`; only the app sets and clears `stale`.
- **Raven Membership Rule** (child rows) — `label`, `provider`, `rule_type`, `status` (Active/Paused), `config` (JSON the provider interprets).
- **Raven Membership Settings** (single record) — one `enabled` switch that turns the whole integration on.

---

## How membership is decided

For any workspace or channel, the app computes the **expected member list**:

1. Take all its **Active** rules (Paused rules are ignored).
2. Ask each rule's provider "who matches?" → get a set of users per rule.
3. Combine those sets using the combinator: **OR** = union, **AND** = intersection.

**Channels narrow their workspace, never widen it:**

```
channel members = (the channel's own rules) ∩ (the workspace's members)
```

So a channel can only contain people who are already in its parent workspace. A channel rule can never sneak someone into the workspace who doesn't belong there.

Then the app compares expected vs. actual and applies the difference: add the missing people, remove the ones who no longer match (but only ones it added itself).

---

## All the paths through the system

### 1. Setup / turning it on
- Admin checks both apps are installed (`is_setup`).
- Admin flips the `enabled` switch (`enable_integration`). **This is one-way — there is no "disable everything" button.** Turning it on kicks off a first full reconcile in the background.

### 2. Creating a managed workspace or channel
- Admin creates a **Workspace Mapping** → the app **creates a brand-new Raven Workspace** behind it (with a guaranteed-unique name).
- Admin creates a **Channel Mapping** under it → the app **creates a brand-new Raven Channel** in that workspace.
- The app never adopts an existing Raven workspace/channel on create — it makes its own. That is also why it never deletes one: it only ever touches records it brought into existence, and even those it leaves standing.

### 3. Real-time sync (event-driven) — the fast path
- Every time *any* document is saved anywhere on the site, a lightweight handler checks: "is this a doctype a provider cares about?"
- If yes (e.g. an enrollment changed), it schedules a sync.
- **Debounced:** a burst of changes (like a bulk import of 500 enrollments) collapses into **one** sync per 30-second window, not 500 syncs.
- The sync re-checks every active mapping and corrects membership.

### 4. Nightly reconcile — the safety net
- Once a day, the app does a full sweep of every active mapping to catch anything the real-time path missed.
- Before sweeping, it first marks **stale mappings** (see below), so a mapping whose Raven record has vanished is already excluded from the sweep in the same run.

### 5. Manual reconcile
- Admin can force a single workspace or channel to re-sync right now (`reconcile_now`), without waiting.

### 6. Previewing before saving
- `preview_rule` — "how many users would this one rule match?" (with a few sample names).
- `compute_rule_diff` — "if I save these new rules, how many get added/removed, and who gets removed?" So an admin sees the impact before committing.

### 7. Deleting a mapping
- Delete a **Workspace Mapping** → its child Channel Mappings are deleted too.
- The **real Raven workspace/channel is left intact** — just no longer managed. Its messages, history and members stay exactly as they were.
- There is no option to do otherwise. **The app never deletes a Raven workspace or channel**, on any code path. "Delete" here only ever means "delete our own mapping record and the rules on it".
- Members the app had added stay in Raven. Nothing evicts them, because eviction only happens as part of a sync, and a deleted mapping is never swept again.

### 8. Stale mappings (someone deleted the Raven side directly)
- An admin can delete a workspace/channel inside Raven directly. The app **allows** that — it deliberately does not block Raven's own delete.
- The mapping now points at a record that is gone. The nightly reconcile detects this and **marks the mapping `stale = 1`** and logs it. The mapping itself is *not* deleted and stays `enabled`.
- A stale mapping stops syncing: `sync_workspace_members` / `sync_channel_members` return `{"skipped": True, "reason": "raven_record_deleted"}` and do nothing else. No crashes, no orphan syncs.
- Everything the admin configured — the rules, the combinator, the label, the type — is still sitting there, intact.

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

- **Never deletes a Raven workspace or channel.** Not when you delete a mapping, not when you uninstall the app, not through any endpoint. The only records it deletes are its own mappings and rules, plus the individual member rows it added.
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
