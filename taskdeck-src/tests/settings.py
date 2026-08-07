SECRET_KEY = "taskdeck-test"
USE_TZ = True
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
}
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_tasks",
    "django_tasks_db",
    "taskdeck",
    "tests",
]
TASKS = {
    "default": {
        "BACKEND": "django_tasks_db.backend.DatabaseBackend",
    }
}
TASKDECK_PROJECT = "testproj"
VERSION = "test-1"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
