from __future__ import annotations

from contextlib import contextmanager

import frappe
from frappe.database import savepoint

from raven_integration.exceptions import RavenAPIError, RavenNotInstalledError
from raven_integration.utils import raven_installed


@contextmanager
def pushing_to_raven():
	"""Suppress the Raven -> mapping reverse-sync handlers for the duration of a
	forward push (mapping -> Raven), so this app's own writes to a Raven workspace or
	channel do not bounce back as a reverse sync. Re-entrant: nested pushes restore
	the previous state, not an unconditional False."""
	token = frappe.flags.get("ri_pushing_to_raven")
	frappe.flags.ri_pushing_to_raven = True
	try:
		yield
	finally:
		frappe.flags.ri_pushing_to_raven = token


def ensure_raven_user(user_id: str) -> None:
	"""Ensure a Raven User record exists for user_id; silently skips disabled users."""
	if not raven_installed():
		return
	user = frappe.get_doc("User", user_id)
	if not user.enabled:
		return
	if frappe.db.exists("Raven User", {"user": user_id}):
		return
	try:
		frappe.get_doc(
			{
				"doctype": "Raven User",
				"user": user_id,
				"full_name": user.full_name or user.first_name or user_id,
				"type": "User",
			}
		).insert(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		# Raven User.user is unique=1; a concurrent sweep (event + nightly
		# reconcile) can pass the exists() check above and both insert. The DB
		# constraint catches the loser — already provisioned, so this is benign.
		return
	except (frappe.PermissionError, RavenAPIError) as e:
		frappe.log_error(
			title=f"{type(e).__name__}: Raven User auto-provision failed for {user_id}",
			message=f"{e}\n\n{frappe.get_traceback()}",
		)


def _adopt_existing_member(member_doctype: str, filters: dict) -> None:
	"""A member row already existed (unique constraint hit) — claim it as rule-managed.

	Without this, a user who joined manually before a rule started matching them
	would keep added_by_rule=0 and never be removed when the rule stops matching.
	Only the channel side claims: there a rule has named the user, which is what
	makes the row the rule's to withdraw later. See add_workspace_member.
	"""
	name = frappe.db.exists(member_doctype, filters)
	if name and not frappe.db.get_value(member_doctype, name, "added_by_rule"):
		frappe.db.set_value(member_doctype, name, "added_by_rule", 1)


def _remove_rule_managed_member(member_doctype: str, filters: dict) -> int:
	"""Delete the member row only if this app added it; manual rows are left alone.

	Returns how many rows went, so a caller counting removals counts writes rather
	than intentions."""
	name = frappe.db.exists(member_doctype, {**filters, "added_by_rule": 1})
	return _delete_member_rows(member_doctype, [name]) if name else 0


def _would_leave_the_workspace_without_an_admin(name: str) -> bool:
	"""Mirrors RavenWorkspaceMember.check_last_admin, which throws in this case.

	It counts admins *other than* the row being deleted, so a workspace whose only
	admin row is this one — or which has no admin at all — refuses the delete. That
	throw comes out of on_trash and takes the whole transaction with it, including
	the mapping delete that a user actually asked for.
	"""
	row = frappe.db.get_value("Raven Workspace Member", name, ["workspace"], as_dict=True)
	if not row:
		return False
	return not frappe.db.count(
		"Raven Workspace Member",
		{"workspace": row.workspace, "is_admin": 1, "name": ("!=", name)},
	)


@contextmanager
def _keeping_channels_unarchived(channels: "list[str]"):
	"""Put back the archive flag Raven sets when the last member of a channel goes.

	RavenChannelMember.after_delete archives a Private channel it has just emptied,
	which reads the last human walking out of a conversation. A withdrawal empties
	it without anyone leaving, and the channel is the one thing the withdrawal
	promises to leave standing. A channel somebody had already archived stays that
	way — un-archiving is not this app's decision either.
	"""
	open_before = [c for c in channels if not frappe.db.get_value("Raven Channel", c, "is_archived")]
	try:
		yield
	finally:
		for channel in open_before:
			if frappe.db.get_value("Raven Channel", channel, "is_archived"):
				frappe.db.set_value("Raven Channel", channel, "is_archived", 0)


def _delete_member_rows(member_doctype: str, names: "list[str]") -> int:
	"""Delete the named member rows. Returns how many went.

	Raven's own hooks run, as they must, but two of them are written for a person
	leaving rather than a withdrawal: a workspace row that would leave no admin
	behind aborts the delete, and the last channel member archives a private
	channel. Skip the first, undo the second.
	"""
	if member_doctype == "Raven Workspace Member":
		names = [n for n in names if not _would_leave_the_workspace_without_an_admin(n)]
		channels = []
	else:
		channels = list({frappe.db.get_value(member_doctype, name, "channel_id") for name in names} - {None})
	with _keeping_channels_unarchived(channels):
		for name in names:
			frappe.delete_doc(member_doctype, name, ignore_permissions=True, force=True)
	return len(names)


def _delete_rule_managed_rows(member_doctype: str, link_field: str, links: "list[str]") -> int:
	"""Delete every rule-managed member row on ``links``. Returns how many went.

	Rows with added_by_rule cleared are left where they are: somebody put them
	there by hand inside Raven, and this app removing them would be the one thing
	it promises never to do.
	"""
	if not links:
		return 0
	return _delete_member_rows(
		member_doctype,
		frappe.get_all(
			member_doctype,
			filters={link_field: ("in", links), "added_by_rule": 1},
			pluck="name",
		),
	)


def _drop_workspace_members_without_a_channel(raven_workspace: str) -> int:
	"""Withdraw the rule-managed workspace rows of anyone now in no channel of it.

	This is the derived-membership rule of expected_workspace_members applied at a
	single moment instead of on a sweep: a workspace member is whoever is in at
	least one channel of the workspace, so someone whose last channel row has just
	gone has nothing left holding them there. Anyone still in a channel — including
	a channel this app does not manage, or one a human added them to — keeps their
	row, and so does anyone whose workspace row is not flagged as this app's.
	"""
	from raven_integration.engine import members_of_workspace_channels

	still_in_a_channel = members_of_workspace_channels(raven_workspace)
	rows = frappe.get_all(
		"Raven Workspace Member",
		filters={"workspace": raven_workspace, "added_by_rule": 1},
		fields=["name", "user"],
	)
	return _delete_member_rows(
		"Raven Workspace Member", [row.name for row in rows if row.user not in still_in_a_channel]
	)


def evict_rule_managed_members(raven_workspace: "str | None", raven_channels: "list[str]") -> dict:
	"""Take back every membership this app's rules granted under one workspace.

	Called when a workspace mapping is deleted. Up to then the app has kept each
	channel's membership matching its conditions, so the people in them are there
	*because* of the mapping — dropping the mapping and leaving them behind would
	leave a channel populated by a rule that no longer exists, with nothing on the
	site able to explain or undo it.

	This is not a Raven delete and does not become one: the workspace, the
	channels and their history are untouched, and so is anyone a human added. Only
	the rows this app inserted itself are withdrawn. (Raven does write one "X was
	removed by Y." system message per row on the way — see add_channel_member for
	why this app cannot ask it not to.)

	Workspace membership is derived from channel membership, so the workspace half
	follows the channel half rather than repeating it: whoever has just lost their
	last channel loses their workspace row too. Withdrawing more than that is not
	only wrong on this app's own rule, it is destructive — Raven answers a deleted
	workspace row by deleting every channel row that user holds anywhere in the
	workspace, including the ones a human added.
	"""
	if not raven_installed():
		return {"channel_members": 0, "workspace_members": 0}
	channels = [c for c in raven_channels if c]
	return {
		"channel_members": _delete_rule_managed_rows("Raven Channel Member", "channel_id", channels),
		"workspace_members": (
			_drop_workspace_members_without_a_channel(raven_workspace) if raven_workspace else 0
		),
	}


def evict_channel_rule_managed_members(raven_workspace: "str | None", raven_channel: "str | None") -> dict:
	"""Take back what one channel's rules granted, when its mapping is deleted.

	The channel half is the workspace-delete withdrawal narrowed to a single
	channel, and for the same reason: those members are in there on the authority
	of rules that are going away with the mapping.

	The workspace half is deliberately *not* the same. The workspace mapping
	survives a channel delete and stays managed, so its membership keeps being
	derived rather than wiped: only the people this channel was the last thing
	keeping in the workspace lose their workspace row, which is exactly what the
	next sync_workspace_members sweep would conclude anyway. Doing it here means a
	disabled or unswept workspace does not sit on membership it can no longer
	account for.

	Safe to call twice, and safe to call after a workspace-level eviction has
	already run — both halves are "delete the rows that are still there", so the
	cascade from a workspace delete finds nothing left to do.
	"""
	if not raven_installed():
		return {"channel_members": 0, "workspace_members": 0}
	return {
		"channel_members": _delete_rule_managed_rows(
			"Raven Channel Member", "channel_id", [raven_channel] if raven_channel else []
		),
		"workspace_members": (
			_drop_workspace_members_without_a_channel(raven_workspace) if raven_workspace else 0
		),
	}


def add_channel_member(channel: str, user: str) -> None:
	"""Add user to a Raven Channel and its parent workspace, flagging the row as rule-managed.

	ignore_system_message suppresses Raven's per-member "X added Y." post, which
	would otherwise put one message in the channel for every member of a synced
	course. Raven honours the flag from v3 onward and ignores it before that, so
	on older Raven the add still succeeds — just noisily.
	"""
	if not raven_installed():
		return
	ensure_raven_user(user)
	workspace = frappe.db.get_value("Raven Channel", channel, "workspace")
	if workspace:
		add_workspace_member(workspace, user)
	try:
		doc = frappe.get_doc(
			{"doctype": "Raven Channel Member", "channel_id": channel, "user_id": user, "added_by_rule": 1}
		)
		doc.flags.ignore_system_message = True
		doc.insert(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		_adopt_existing_member("Raven Channel Member", {"channel_id": channel, "user_id": user})


def remove_channel_member(channel: str, user: str) -> int:
	"""Remove a rule-managed channel member row; leaves manually-added rows untouched.

	Returns how many rows went."""
	if not raven_installed():
		return 0
	return _remove_rule_managed_member("Raven Channel Member", {"channel_id": channel, "user_id": user})


def add_workspace_member(workspace: str, user: str) -> None:
	"""Add user to a Raven Workspace, flagging the row as rule-managed.

	A row that is already there is left exactly as it is, unlike the channel one.
	Nothing here is evidence that this app should own it: workspace membership is
	derived from channel membership, so the only thing being in a channel says
	about a workspace row somebody else wrote — a hand-added member, or the admin
	row Raven writes for whoever created the workspace — is that it should stay.
	"""
	if not raven_installed():
		return
	ensure_raven_user(user)
	try:
		frappe.get_doc(
			{"doctype": "Raven Workspace Member", "workspace": workspace, "user": user, "added_by_rule": 1}
		).insert(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		return
	except frappe.ValidationError as e:
		# Raven raises a bare ValidationError for its own duplicate-member check, so
		# the row itself, not the exception class, is what says this was a duplicate.
		# Everything else a ValidationError can mean here — a user with no Raven User
		# for the reqd link to resolve, a missing field — is a member this app failed
		# to add, and used to be indistinguishable from one it already had.
		if frappe.db.exists("Raven Workspace Member", {"workspace": workspace, "user": user}):
			return
		frappe.log_error(
			title=f"{type(e).__name__}: Raven workspace member add failed for {user}",
			message=f"workspace {workspace}: {e}\n\n{frappe.get_traceback()}",
		)


def _still_in_a_channel_of(raven_workspace: str, user: str) -> bool:
	"""Re-read, under a lock, whether the user holds any channel row in the workspace.

	The expected set a removal is decided from was read at the top of the sweep, and
	Raven answers a deleted workspace row by wiping every channel row that user holds
	in the workspace — added_by_rule or not, in channels this app never mapped. So a
	row a human added in Raven in between is destroyed by a decision taken before it
	existed. The locking read is what closes that window: it reads the latest
	committed row rather than this transaction's snapshot, and holds the gap until
	commit. It runs on the unique (channel_id, user_id) index Raven adds.

	It matches nothing almost every time it runs — `to_remove` is built from people
	who are in no channel — so what it takes on that index are gap locks rather than
	row locks. They are the price of reading past the snapshot; what keeps them
	affordable is that `events.resync_all` commits per mapping, so they live for one
	mapping instead of for the whole nightly sweep.
	"""
	channels = frappe.get_all("Raven Channel", filters={"workspace": raven_workspace}, pluck="name")
	if not channels:
		return False
	return bool(
		frappe.db.get_value(
			"Raven Channel Member",
			{"channel_id": ("in", channels), "user_id": user},
			"name",
			for_update=True,
		)
	)


def remove_workspace_member(workspace: str, user: str) -> int:
	"""Remove a rule-managed workspace member row; leaves manually-added rows untouched.

	Returns how many rows went — none, if the user turns out to still be in a channel
	of the workspace, which is the whole of what a workspace row means here."""
	if not raven_installed():
		return 0
	if _still_in_a_channel_of(workspace, user):
		return 0
	return _remove_rule_managed_member("Raven Workspace Member", {"workspace": workspace, "user": user})


def _apply_one_member(action, raven_record: str, user: str, member_doctype: str, verb: str) -> bool:
	"""Run one member's add or remove in its own savepoint. True if a row was written.

	One member the site cannot write is not a reason to abandon the rest of a diff.
	ensure_raven_user leaves a disabled user without a Raven User deliberately, and
	the member row's user link is reqd, so their insert throws LinkValidationError —
	which is a ValidationError, not the NameError the duplicate catch covers. It used
	to escape this loop with every later add and the whole removal pass still
	pending, on every sweep, silently. The savepoint is what makes carrying on safe:
	a bare except would leave whatever the failed member had already written (its
	workspace row, from add_channel_member) sitting in the transaction. The log is
	written after the rollback, or it would be rolled back with it.
	"""
	failure = None
	# frappe.msgprint appends to frappe.message_log before frappe.throw raises, so
	# swallowing the exception without dropping what it queued returns HTTP 200
	# carrying _server_messages, and the settings page pops a red error dialog on a
	# request that succeeded. Only what this member queued is dropped — the caller's
	# own messages are not ours to discard. Same reasoning as engine._record_skipped.
	messages_before = len(frappe.message_log)
	with savepoint(catch=Exception):
		try:
			written = action(raven_record, user)
		except Exception as e:
			failure = f"{type(e).__name__}: {e}\n\n{frappe.get_traceback()}"
			raise
		# The removers report how many rows went; the adders report nothing.
		return written is None or bool(written)
	while len(frappe.message_log) > messages_before:
		frappe.clear_last_message()
	frappe.log_error(
		# Error Log.method is a 140-char Data field and a Raven channel name is a
		# whole course title; the log itself must not become the thing that throws.
		title=f"Raven member {verb} skipped: {user} on {member_doctype} {raven_record}"[:140],
		message=failure,
	)
	return False


def _sync_rule_managed_members(
	*,
	mapping_doctype: str,
	mapping_name: str,
	link_field: str,
	no_link_reason: str,
	expected_members,
	member_doctype: str,
	member_link_field: str,
	member_user_field: str,
	add,
	remove,
	rule_gated: bool = True,
	claims_existing: bool = True,
	enabled_gated: bool = True,
	after_synced=None,
	cache: dict | None = None,
) -> dict:
	"""Diff-apply one mapping's rule-managed membership onto its Raven record.

	All three switches are off for a workspace, which carries no rules:

	``rule_gated`` — its membership is derived from its channels, so the two rule
	guards below (skip when nothing is active, freeze whoever a disabled rule put
	here) have nothing to read and no meaning.

	``claims_existing`` — the expected set is every channel member, which says who
	is in a channel and nothing about who put them there, so a row already sitting
	in the workspace is not this app's to claim. Excluding those rows from the diff
	is also what stops the sweep re-proposing them forever: the insert would fail
	Raven's duplicate check every time, and the throw lands in the message log of
	whatever request drove the sweep.

	``enabled_gated`` — a workspace mapping has no ``enabled`` field to read. Only
	a channel can be switched off, and switching every channel off is what stops a
	workspace: its derived membership empties out because the channels stop
	feeding it, not because the workspace itself was ever gated.
	"""
	if not raven_installed():
		return {"skipped": True, "reason": "raven_not_installed"}
	# Imported late: events imports this module. The global kill switch has to be read
	# here as well as in events.notify_change and scheduler.reconcile_all, because
	# api._enqueue_member_sync queues sync_channel_members straight — so an admin who
	# switched the integration off was still getting adds and removals from it.
	from raven_integration.events import is_active

	if not is_active():
		return {"skipped": True, "reason": "integration_disabled"}
	if enabled_gated and not frappe.db.get_value(mapping_doctype, mapping_name, "enabled"):
		return {"skipped": True, "reason": "disabled"}
	from raven_integration.engine import disabled_rule_members, has_active_rules

	mapping = frappe.get_doc(mapping_doctype, mapping_name)
	if mapping.stale:
		return {"skipped": True, "reason": "raven_record_deleted"}
	if rule_gated and not has_active_rules(mapping.member_rules_json):
		return {"skipped": True, "reason": "no_active_rules"}
	expected = expected_members(mapping_name, cache=cache)
	raven_record = frappe.db.get_value(mapping_doctype, mapping_name, link_field)
	if not raven_record:
		return {"skipped": True, "reason": no_link_reason}
	current_rule_managed = set(
		frappe.get_all(
			member_doctype,
			filters={member_link_field: raven_record, "added_by_rule": 1},
			pluck=member_user_field,
		)
	)
	# Members a disabled rule put here stay put; it just stops granting membership.
	frozen = current_rule_managed & disabled_rule_members(mapping.member_rules_json) if rule_gated else set()
	already_there = (
		current_rule_managed
		if claims_existing
		else set(
			frappe.get_all(member_doctype, filters={member_link_field: raven_record}, pluck=member_user_field)
		)
	)
	to_add = expected - already_there
	to_remove = current_rule_managed - expected - frozen
	added = removed = 0
	for u in to_add:
		if not _apply_one_member(add, raven_record, u, member_doctype, "add"):
			continue
		added += 1
		if after_synced:
			after_synced(raven_record, u, "added", "event")
	for u in to_remove:
		if not _apply_one_member(remove, raven_record, u, member_doctype, "remove"):
			continue
		removed += 1
		if after_synced:
			after_synced(raven_record, u, "removed", "event")
	return {"added": added, "removed": removed}


def sync_channel_members(channel_name: str, *, cache: dict | None = None) -> dict:
	"""Diff-apply rule-managed channel membership for the given Raven Channel Mapping."""
	from raven_integration.engine import expected_channel_members

	return _sync_rule_managed_members(
		mapping_doctype="Raven Channel Mapping",
		mapping_name=channel_name,
		link_field="raven_channel",
		no_link_reason="no_raven_channel_link",
		expected_members=expected_channel_members,
		member_doctype="Raven Channel Member",
		member_link_field="channel_id",
		member_user_field="user_id",
		add=add_channel_member,
		remove=remove_channel_member,
		# Channel membership is the event other apps subscribe to; workspace
		# membership is a side effect of it and fires no hooks.
		after_synced=_fire_after_synced,
		cache=cache,
	)


def sync_workspace_members(workspace_name: str, *, cache: dict | None = None) -> dict:
	"""Reconcile derived workspace membership for the given Raven Workspace Mapping.

	The expected set is whoever is in one of the workspace's channels, so this only
	ever tidies up after the channel sweep: it drops the rule-managed workspace rows
	of people who have just lost their last channel, and re-adds anyone a channel
	holds who somehow has no workspace row.
	"""
	from raven_integration.engine import expected_workspace_members

	return _sync_rule_managed_members(
		mapping_doctype="Raven Workspace Mapping",
		mapping_name=workspace_name,
		link_field="raven_workspace",
		no_link_reason="no_raven_workspace_link",
		expected_members=expected_workspace_members,
		member_doctype="Raven Workspace Member",
		member_link_field="workspace",
		member_user_field="user",
		add=add_workspace_member,
		remove=remove_workspace_member,
		rule_gated=False,
		claims_existing=False,
		enabled_gated=False,
		cache=cache,
	)


def _ensure_creator_is_a_raven_user() -> None:
	"""Provision the acting user in Raven before creating a workspace or channel.

	Raven makes whoever creates one its first admin — Raven Workspace.after_insert
	and Raven Channel.after_insert both insert a member row for the session user —
	and those rows link to Raven User, not User. A manager who is not a Raven user
	yet (LMS's Moderator role has desk_access = 0, so such a user is a Website User
	and Raven's auto-add for System Users never fires) would otherwise fail the
	create with "Could not find User". Provisioning is what this app already does for
	every member it adds.
	"""
	if frappe.session.user in ("Guest", "Administrator"):
		return
	ensure_raven_user(frappe.session.user)


def create_raven_workspace_for(ws_map) -> None:
	"""Called from RavenWorkspaceMapping.before_insert to create the backing Raven Workspace."""
	if not raven_installed():
		raise RavenNotInstalledError("Raven is not installed on this site")
	_ensure_creator_is_a_raven_user()
	try:
		with pushing_to_raven():
			rw = _insert_unique_raven_doc(
				{"doctype": "Raven Workspace", "type": ws_map.workspace_type},
				"workspace_name",
				ws_map.workspace_label,
			)
	except RavenAPIError:
		raise
	except Exception as e:
		raise RavenAPIError(f"{type(e).__name__}: {e}") from e
	ws_map.raven_workspace = rw.name


def create_raven_channel_for(ch_map) -> None:
	"""Called from RavenChannelMapping.before_insert to create the backing Raven Channel."""
	if not raven_installed():
		raise RavenNotInstalledError("Raven is not installed on this site")
	_ensure_creator_is_a_raven_user()
	parent = frappe.get_doc("Raven Workspace Mapping", ch_map.workspace)
	if not parent.raven_workspace:
		raise RavenAPIError(f"Parent workspace {parent.name} has no Raven workspace link")
	try:
		with pushing_to_raven():
			rc = _insert_unique_raven_doc(
				{
					"doctype": "Raven Channel",
					"workspace": parent.raven_workspace,
					"type": ch_map.channel_type,
				},
				"channel_name",
				ch_map.channel_label,
			)
	except RavenAPIError:
		raise
	except Exception as e:
		raise RavenAPIError(f"{type(e).__name__}: {e}") from e
	ch_map.raven_channel = rc.name


def push_workspace_type_to_raven(ws_map) -> None:
	"""Propagate a workspace-mapping visibility change to its backing Raven Workspace.

	Skipped when Raven is absent, the mapping is stale, or it has no backing Raven
	workspace — matching the rename helpers. Saved through the doc so Raven's own
	side effects of a type change run."""
	if not raven_installed() or ws_map.stale or not ws_map.raven_workspace:
		return
	rw = frappe.get_doc("Raven Workspace", ws_map.raven_workspace)
	if rw.type == ws_map.workspace_type:
		return
	rw.type = ws_map.workspace_type
	with pushing_to_raven():
		rw.save(ignore_permissions=True)


def push_channel_type_to_raven(ch_map) -> None:
	"""Propagate a channel-mapping visibility change to its backing Raven Channel.

	Skipped when Raven is absent, the mapping is stale, or it has no backing Raven
	channel."""
	if not raven_installed() or ch_map.stale or not ch_map.raven_channel:
		return
	rc = frappe.get_doc("Raven Channel", ch_map.raven_channel)
	if rc.type == ch_map.channel_type:
		return
	rc.type = ch_map.channel_type
	with pushing_to_raven():
		rc.save(ignore_permissions=True)


def _insert_unique_raven_doc(props: dict, fieldname: str, label: str, attempts: int = 5):
	"""Insert a Raven doc whose ``fieldname`` must be unique, retrying with a fresh
	suffixed name until one is accepted.

	The upfront probe cannot be trusted on its own: Raven slugifies
	Raven Channel.channel_name in before_validate (strip/lower/spaces to hyphens),
	so probing the raw label never matches a stored name and the same colliding
	name would otherwise be re-proposed on every attempt. Each attempted name is
	therefore skipped on the next pass, which makes progress without this app
	having to reimplement Raven's normalisation.

	Raven raises a bare ValidationError for its per-workspace duplicate check, not
	UniqueValidationError, so the collision catch has to be broad. A ValidationError
	that is not a collision simply fails every attempt and is re-raised."""
	doctype = props["doctype"]
	tried: set[str] = set()
	last_error: Exception | None = None
	for _ in range(attempts):
		name = _unique_raven_name(label, doctype, fieldname, skip=tried)
		tried.add(name)
		doc = frappe.get_doc({**props, fieldname: name})
		try:
			return doc.insert(ignore_permissions=True)
		except (frappe.UniqueValidationError, frappe.DuplicateEntryError, frappe.ValidationError) as e:
			last_error = e
	raise RavenAPIError(
		f"Could not allocate a unique {fieldname} for {label!r} after {attempts} attempts: {last_error}"
	) from last_error


def _unique_raven_name(label: str, doctype: str, fieldname: str, skip: "set[str] | None" = None) -> str:
	"""Return label, appending (2), (3), … until it is unique in doctype.fieldname.

	``skip`` holds names already attempted and rejected by Raven this call."""
	skip = skip or set()
	base = label
	n = 1
	while base in skip or frappe.db.exists(doctype, {fieldname: base}):
		n += 1
		base = f"{label} ({n})"
	return base


def _fire_after_synced(channel: str, user: str, action: str, source: str) -> None:
	"""Invoke every after_raven_member_synced hook, logging failures without re-raising."""
	for handler in frappe.get_hooks("after_raven_member_synced"):
		try:
			frappe.get_attr(handler)(channel=channel, user=user, action=action, source=source)
		except Exception as e:
			frappe.log_error(
				title=f"{type(e).__name__}: after_raven_member_synced handler {handler!r} failed",
				message=f"{e}\n\n{frappe.get_traceback()}",
			)
