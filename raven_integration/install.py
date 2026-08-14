from raven_integration.custom_fields import ensure_added_by_rule_field, remove_added_by_rule_field
from raven_integration.permissions import remove_manager_docperms, sync_manager_docperms


def after_install():
	ensure_added_by_rule_field()
	sync_manager_docperms()


def after_migrate():
	ensure_added_by_rule_field()
	sync_manager_docperms()


def before_uninstall():
	remove_added_by_rule_field()
	remove_manager_docperms()
