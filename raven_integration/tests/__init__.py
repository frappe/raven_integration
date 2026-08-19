from unittest.mock import patch

_SWEEP_TRANSACTION_SEAMS = (
	"raven_integration.events._commit_step",
	"raven_integration.events._rollback_step",
)


def hold_one_transaction(case) -> None:
	"""Make `resync_all` run its whole sweep inside the caller's transaction.

	The sweep commits per mapping and rolls back a mapping it could not apply. Both
	are wrong inside a test: the commit strands the case's fixtures on the site, and
	the rollback destroys them. Patching the two seams out is what lets a test call
	the real sweep at all; the boundaries themselves are pinned by asserting on these
	same seams in TestSweepTransactionBoundaries, not by letting them run.
	"""
	for target in _SWEEP_TRANSACTION_SEAMS:
		patcher = patch(target)
		patcher.start()
		case.addCleanup(patcher.stop)
