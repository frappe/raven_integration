class RavenNotInstalledError(Exception):
	"""Raised when sync is invoked but Raven is not installed on this site."""


class RavenAPIError(Exception):
	"""Raised when a call into Raven (create workspace/channel/member, etc.) fails."""


class ProviderDataError(Exception):
	"""Raised when provider-side data needed for sync is missing or malformed."""
