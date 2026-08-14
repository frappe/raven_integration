app_name = "raven_integration"
app_title = "Raven Integration"
app_publisher = "Frappe"
app_description = "Keep Raven workspace/channel membership in sync with rules from any app"
app_email = "raizasafeel@gmail.com"
app_license = "mit"

# Apps
# ------------------

required_apps = ["raven"]

after_install = [
	"raven_integration.install.after_install",
	"raven_integration.events.clear_trigger_doctypes_cache",
]
after_migrate = [
	"raven_integration.install.after_migrate",
	"raven_integration.events.clear_trigger_doctypes_cache",
]
before_uninstall = "raven_integration.install.before_uninstall"

scheduler_events = {
	"daily": [
		"raven_integration.scheduler.reconcile_all",
	],
}

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "raven_integration",
# 		"logo": "/assets/raven_integration/logo.png",
# 		"title": "Raven Integration",
# 		"route": "/raven_integration",
# 		"has_permission": "raven_integration.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/raven_integration/css/raven_integration.css"
# app_include_js = "/assets/raven_integration/js/raven_integration.js"

# include js, css files in header of web template
# web_include_css = "/assets/raven_integration/css/raven_integration.css"
# web_include_js = "/assets/raven_integration/js/raven_integration.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "raven_integration/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "raven_integration/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "raven_integration.utils.jinja_methods",
# 	"filters": "raven_integration.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "raven_integration.install.before_install"
# after_install = "raven_integration.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "raven_integration.uninstall.before_uninstall"
# after_uninstall = "raven_integration.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "raven_integration.utils.before_app_install"
#
# Providers are declared in other apps' hooks.py, so the memoized trigger-doctype
# set goes stale the moment one of those apps arrives or leaves. Both hooks are
# called with the name of the app being installed/uninstalled.
#
# Manager roles come from other apps' hooks.py too, so a host app installed after
# this one needs its DocPerms granted at that point rather than at our own install.
after_app_install = [
	"raven_integration.events.clear_trigger_doctypes_cache",
	"raven_integration.permissions.sync_manager_docperms",
]

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "raven_integration.utils.before_app_uninstall"
after_app_uninstall = ["raven_integration.events.clear_trigger_doctypes_cache"]

# Build
# ------------------
# To hook into the build process

# after_build = "raven_integration.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "raven_integration.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events
#
# A single wildcard handler reacts to every save site-wide and forwards only the
# doctypes that some registered provider declared in its `triggers` list.
#
# The per-doctype entries below coexist with the `"*"` entry rather than replacing
# it: frappe.model.document runs `doc_events[doctype][method] + doc_events["*"][method]`,
# so a Raven Workspace/Channel delete fires mark_mappings_stale first and then the
# wildcard handler. Distinct handlers, so nothing is run twice.
doc_events = {
	"*": {
		"after_insert": "raven_integration.events.on_provider_doc_change",
		"on_update": "raven_integration.events.on_provider_doc_change",
		"on_trash": "raven_integration.events.on_provider_doc_change",
	},
	"Raven Workspace": {
		"on_trash": "raven_integration.events.mark_mappings_stale",
		# Mirror a name / visibility change made inside Raven back onto the mapping.
		"on_update": "raven_integration.events.sync_workspace_type_from_raven",
		"after_rename": "raven_integration.events.sync_workspace_rename_from_raven",
	},
	"Raven Channel": {
		"on_trash": "raven_integration.events.mark_mappings_stale",
	},
}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"raven_integration.tasks.all"
# 	],
# 	"daily": [
# 		"raven_integration.tasks.daily"
# 	],
# 	"hourly": [
# 		"raven_integration.tasks.hourly"
# 	],
# 	"weekly": [
# 		"raven_integration.tasks.weekly"
# 	],
# 	"monthly": [
# 		"raven_integration.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "raven_integration.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "raven_integration.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "raven_integration.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "raven_integration.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------
# The mappings hold a read-only Link to the Raven Workspace/Channel they manage.
# Without this, deleting a workspace/channel inside Raven raises LinkExistsError
# and the integration would block Raven's own delete. Exempting the mappings lets
# the Raven-side delete succeed; the nightly reconcile then disables the now-
# dangling mapping (see scheduler.detect_dangling_links).
ignore_links_on_delete = ["Raven Workspace Mapping", "Raven Channel Mapping"]

# Request Events
# ----------------
# before_request = ["raven_integration.utils.before_request"]
# after_request = ["raven_integration.utils.after_request"]

# Job Events
# ----------
# before_job = ["raven_integration.utils.before_job"]
# after_job = ["raven_integration.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"raven_integration.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
export_python_type_annotations = True

# Require all whitelisted methods to have type annotations
require_type_annotated_api_methods = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

# Membership Providers
# --------------------
# Apps register providers by appending dotted paths to get_provider callables.
raven_membership_providers = []

# Membership Sync Events
# ----------------------
# Fired once per member row the sync adds or removes. Handlers run in
# installed_apps order and every failure is logged and swallowed, so a handler
# can never abort a sweep. Contract and example: see README.md.
after_raven_member_synced = []
