from django.apps import AppConfig


class TaskdeckConfig(AppConfig):
    name = "taskdeck"
    verbose_name = "taskdeck"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from taskdeck.signals import connect  # noqa: PLC0415 - Django app-ready hook

        connect()
