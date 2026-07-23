from raven_integration.custom_fields import ensure_added_by_rule_field, remove_added_by_rule_field


def after_install():
	ensure_added_by_rule_field()


def after_migrate():
	ensure_added_by_rule_field()


def before_uninstall():
	remove_added_by_rule_field()
