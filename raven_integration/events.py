from __future__ import annotations

from contextlib import suppress

import frappe
import redis

from raven_integration.exceptions import ProviderDataError, RavenAPIError
from raven_integration.sync_service import sync_channel_members, sync_workspace_members
from raven_integration.utils import raven_installed

_DEBOUNCE_KEY = "raven_integration:pending_resync"
_DEBOUNCE_SECONDS = 30
_GENERATION_KEY = "raven_integration:change_generation"
_GENERATION_TTL = 24 * 60 * 60
_TRIGGER_DOCTYPES_KEY = "raven_integration:trigger_doctypes"
_TRIGGER_DOCTYPES_TTL = 60 * 60

# Raven doctype -> (mapping doctype that points at it, the link fieldname).
_RAVEN_BACKED_MAPPINGS = {
	"Raven Workspace": ("Raven Workspace Mapping", "raven_workspace"),
	"Raven Channel": ("Raven Channel Mapping", "raven_channel"),
}


def is_active() -> bool:
	"""True when membership sync should run: the integration must be enabled
	(one-way `enabled` flag on Raven Membership Settings) AND Raven installed."""
	return bool(frappe.db.get_single_value("Raven Membership Settings", "enabled")) and raven_installed()


def _sweepable_channels(swept_workspaces: list[str]) -> tuple[list[str], int]:
	"""Enabled channel mappings whose parent workspace mapping still exists.

	Syncing a channel joins its members to the parent Raven workspace too, so a
	channel whose workspace mapping is gone strands rule-managed workspace rows
	that nothing will ever reconcile. Deleting a workspace cascades its channels,
	so this should find nothing; it is the guard for the case where that cascade
	did not complete, not a state the UI can produce.
	"""
	channels = frappe.get_all("Raven Channel Mapping", filters={"enabled": 1}, fields=["name", "workspace"])
	swept = set(swept_workspaces)
	names = [c.name for c in channels if c.workspace in swept]
	return names, len(channels) - len(names)


def resync_all() -> dict:
	"""Diff-apply membership for every enabled channel and every workspace mapping.

	Channels are swept first, and that order is load-bearing twice over. A workspace's
	membership is *derived* from who is in its channels, so it can only be computed
	once the channels are correct. And the pair is torn down leaf-first: adding a
	channel member joins the parent workspace before the channel, so removals must
	run in the mirror order or a failed workspace pass would leave a channel member
	who is not a member of the channel's own workspace.
	"""
	# Every workspace mapping, unfiltered: a workspace has no on/off of its own.
	# What can be switched off is a channel, and a workspace with every channel
	# disabled sweeps to an empty expected set on its own.
	workspaces = frappe.get_all("Raven Workspace Mapping", pluck="name")
	channels, channels_skipped = _sweepable_channels(workspaces)
	added = removed = errors = 0
	cache: dict = {}

	for kind, names, sync in (
		("channel", channels, sync_channel_members),
		("workspace", workspaces, sync_workspace_members),
	):
		for name in names:
			try:
				r = sync(name, cache=cache)
				added += r.get("added", 0)
				removed += r.get("removed", 0)
			except (RavenAPIError, ProviderDataError) as e:
				errors += 1
				frappe.log_error(
					title=f"Resync {type(e).__name__}: {kind} {name}",
					message=f"{e}\n\n{frappe.get_traceback()}",
				)
			except Exception:
				errors += 1
				frappe.log_error(
					title=f"Resync unexpected error: {kind} {name}",
					message=frappe.get_traceback(),
				)

	return {
		"channels_processed": len(channels),
		"channels_skipped_orphaned": channels_skipped,
		"workspaces_processed": len(workspaces),
		"added": added,
		"removed": removed,
		"errors": errors,
	}


def _bump_change_generation() -> None:
	"""Move the change counter one on.

	Raw INCR against the made key: it is atomic, so two workers cannot lose each
	other's bump, and it stays out of the process-local dict get_value/set_value keep
	in front of redis — a worker has to see a bump another process made. Swallows a
	cache outage the way set_value and delete_value do; this runs on commit and must
	not turn a saved document into a 500.
	"""
	cache = frappe.cache()
	key = cache.make_key(_GENERATION_KEY)
	with suppress(redis.exceptions.ConnectionError):
		cache.incr(key)
		cache.expire(key, _GENERATION_TTL)


def _change_generation() -> bytes | None:
	cache = frappe.cache()
	with suppress(redis.exceptions.ConnectionError):
		return cache.get(cache.make_key(_GENERATION_KEY))


def notify_change() -> None:
	"""Provider event entrypoint: a membership-relevant record changed somewhere."""
	if not is_active():
		return
	cache = frappe.cache()
	# Bumped on commit rather than here, so the counter moves only once this change is
	# visible to a reader and a sweep that snapshotted before the commit can tell it
	# read stale rows. Registered before the enqueue below, which is an after_commit
	# callback too, so the bump is always in place before the job can start.
	frappe.db.after_commit.add(_bump_change_generation)
	if cache.get_value(_DEBOUNCE_KEY):
		return
	cache.set_value(_DEBOUNCE_KEY, "1", expires_in_sec=_DEBOUNCE_SECONDS)
	# The key lands in redis immediately but the sweep it guards is only queued on
	# commit, so a rollback would strand it and silently swallow every change until
	# it expires.
	frappe.db.after_rollback.add(lambda: cache.delete_value(_DEBOUNCE_KEY))
	frappe.enqueue(
		"raven_integration.events.run_resync",
		queue="short",
		enqueue_after_commit=True,
	)


def run_resync() -> None:
	"""Background job target for the debounced event path."""
	generation = _change_generation()
	frappe.cache().delete_value(_DEBOUNCE_KEY)
	resync_all()
	# A change that arrived while the key above was still set queued nothing of its
	# own, and this job is one transaction under REPEATABLE READ — its commit can land
	# after the sweep read its rows and be missed by both. The counter having moved is
	# the proof, and notify_change() coalesces the second pass with anything already
	# scheduled.
	if _change_generation() != generation:
		notify_change()


def _trigger_doctypes() -> set[str]:
	"""Doctypes registered providers react to.

	Memoized in redis because on_provider_doc_change consults it on every save
	site-wide. A provider lives in another app's hooks.py, so the set changes when
	an app is installed, uninstalled or migrated — clear_trigger_doctypes_cache is
	wired to all four hooks, and the TTL is the backstop for anything that
	bypasses them (a hooks.py edited in place, a restored site).
	"""
	cache = frappe.cache()
	try:
		doctypes = cache.get_value(_TRIGGER_DOCTYPES_KEY)
		if doctypes is None:
			from raven_integration import registry

			doctypes = sorted(registry.trigger_doctypes())
			cache.set_value(_TRIGGER_DOCTYPES_KEY, doctypes, expires_in_sec=_TRIGGER_DOCTYPES_TTL)
		return set(doctypes)
	except Exception:
		# Fail closed, but never quietly: an app whose hooks.py raises on import, or a
		# cache that is down, otherwise turns the change handler into a site-wide no-op
		# and membership stops reacting to anything until the nightly reconcile.
		frappe.log_error(
			title="raven_integration trigger doctypes",
			message=frappe.get_traceback(),
		)
		return set()


def clear_trigger_doctypes_cache(app_name: str | None = None) -> None:
	"""Drop the memoized provider trigger set so the next save rebuilds it.

	``app_name`` is what the app hooks pass; it is ignored because any app's
	hooks.py may register a provider.
	"""
	frappe.cache().delete_value(_TRIGGER_DOCTYPES_KEY)


def mark_mappings_stale(doc, method=None) -> None:
	"""doc_events on_trash for Raven Workspace / Raven Channel.

	Raven owns its workspaces and channels; this app never deletes one. When Raven
	deletes it out from under a mapping, the mapping is kept (its rules are the
	user's work) but flagged so sync stops and the UI can offer a recreate.

	Writes the flag with db.set_value rather than saving the mapping: the mapping's
	own before_insert/validate exists to provision and check the Raven record, and
	must not run for a record that has just gone away. Fail-safe — a failure here
	must never abort Raven's own delete; the nightly reconcile is the backstop.
	"""
	try:
		mapping_doctype, link_field = _RAVEN_BACKED_MAPPINGS.get(doc.doctype, (None, None))
		if not mapping_doctype:
			return
		for name in frappe.get_all(mapping_doctype, filters={link_field: doc.name, "stale": 0}, pluck="name"):
			frappe.db.set_value(mapping_doctype, name, "stale", 1, update_modified=False)
	except Exception:
		frappe.log_error(
			title="raven_integration mark_mappings_stale",
			message=frappe.get_traceback(),
		)


def sync_workspace_type_from_raven(doc, method=None) -> None:
	"""doc_events on_update for Raven Workspace: mirror a visibility change made
	inside Raven back onto the mapping, so the two never disagree.

	Written with db.set_value, not a mapping save: the mapping's on_update would push
	the type straight back to Raven, and its validate/before_insert have no business
	running here. Skipped while this app is itself pushing to Raven (loop guard), and
	fail-safe — a failure here must never abort Raven's own save.
	"""
	if frappe.flags.get("ri_pushing_to_raven"):
		return
	try:
		mapping = frappe.db.get_value(
			"Raven Workspace Mapping",
			{"raven_workspace": doc.name},
			["name", "workspace_type"],
			as_dict=True,
		)
		if not mapping or mapping.workspace_type == doc.type:
			return
		frappe.db.set_value(
			"Raven Workspace Mapping", mapping.name, "workspace_type", doc.type, update_modified=False
		)
	except Exception:
		frappe.log_error(
			title="raven_integration sync_workspace_type_from_raven",
			message=frappe.get_traceback(),
		)


def sync_workspace_rename_from_raven(doc, method=None, old_name=None, new_name=None, merge=False) -> None:
	"""doc_events after_rename for Raven Workspace: mirror a rename made inside Raven
	onto the mapping's label and its own docname.

	frappe.rename_doc has already cascaded the new name into the mapping's
	raven_workspace Link by the time this runs, so the mapping is found by that link.
	Reuses _set_mapping_label so the mapping docname stays truthful (RWM-{label}) and
	collisions are reported the same way the forward path reports them. Skipped while
	this app is itself renaming Raven (loop guard) and on a merge; fail-safe.
	"""
	if frappe.flags.get("ri_pushing_to_raven") or merge:
		return
	try:
		from raven_integration.api import _set_mapping_label

		mapping = frappe.db.get_value("Raven Workspace Mapping", {"raven_workspace": new_name}, "name")
		if not mapping:
			return
		_set_mapping_label("Raven Workspace Mapping", mapping, new_name)
	except Exception:
		frappe.log_error(
			title="raven_integration sync_workspace_rename_from_raven",
			message=frappe.get_traceback(),
		)


def on_provider_doc_change(doc, method=None) -> None:
	"""Wildcard doc_events handler: if doc's doctype is a registered provider
	trigger, schedule a (debounced) membership resync. Fail-safe: never raises.

	Runs on EVERY document save site-wide, so it stays cheap (one cached set lookup).
	"""
	try:
		if doc.doctype not in _trigger_doctypes():
			return
		notify_change()
	except Exception:
		# Never let membership sync break an unrelated document save.
		frappe.log_error(
			title="raven_integration on_provider_doc_change",
			message=frappe.get_traceback(),
		)
