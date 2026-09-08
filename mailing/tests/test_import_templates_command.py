import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from mailing.models import Client, EmailTemplate, Organization

pytestmark = pytest.mark.django_db(transaction=True)

WELCOME_MD = """---
subject: "Welcome, {{ user_name }}"
name: Welcome email
category: onboarding
required_context:
  - name: user_name
    description: "Recipient name."
  - verify_url
example_context:
  user_name: Ada
  verify_url: https://example.com/verify?token=demo
---

# Welcome, {{ user_name }}!

Confirm your address: {{ verify_url }}
"""

REMINDER_MD = """---
subject: "Reminder for {{ user_name }}"
---

Do not forget, {{ user_name }}.
"""

MISSING_SUBJECT_MD = """---
category: onboarding
---

No subject here.
"""


@pytest.fixture
def client_record():
    organization = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    return Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")


def write_templates(tmp_path, **files):
    for name, content in files.items():
        (tmp_path / f"{name}.md").write_text(content)


def test_import_creates_one_draft_per_file_with_frontmatter_fields(tmp_path, client_record):
    write_templates(tmp_path, welcome=WELCOME_MD, reminder=REMINDER_MD)

    call_command("import_templates", "--dir", str(tmp_path), "--client", "dtc-courses")

    welcome = EmailTemplate.objects.get(client=client_record, key="welcome")
    assert welcome.name == "Welcome email"
    assert welcome.subject == "Welcome, {{ user_name }}"
    assert welcome.category == "onboarding"
    assert welcome.markdown_body.startswith("# Welcome")
    assert welcome.required_context == [
        {"name": "user_name", "description": "Recipient name."},
        {"name": "verify_url", "description": ""},
    ]
    assert welcome.example_context["user_name"] == "Ada"
    assert welcome.is_transactional is True
    assert welcome.is_active is True
    assert welcome.html_body == ""

    reminder = EmailTemplate.objects.get(client=client_record, key="reminder")
    assert reminder.name == "reminder"
    assert reminder.category == ""
    assert reminder.required_context == []


def test_import_is_rerunnable_as_draft_update(tmp_path, client_record):
    write_templates(tmp_path, welcome=WELCOME_MD)

    call_command("import_templates", "--dir", str(tmp_path), "--client", "dtc-courses")
    call_command("import_templates", "--dir", str(tmp_path), "--client", "dtc-courses")

    assert EmailTemplate.objects.filter(client=client_record).count() == 1


def test_import_reports_errors_and_fails_the_run(tmp_path, client_record):
    write_templates(tmp_path, welcome=WELCOME_MD, broken=MISSING_SUBJECT_MD)

    with pytest.raises(CommandError, match="1 template file"):
        call_command("import_templates", "--dir", str(tmp_path), "--client", "dtc-courses")

    assert EmailTemplate.objects.filter(client=client_record, key="welcome").exists()
    assert not EmailTemplate.objects.filter(client=client_record, key="broken").exists()


def test_import_rejects_unknown_client(tmp_path, client_record):
    write_templates(tmp_path, welcome=WELCOME_MD)
    with pytest.raises(CommandError, match="Client not found: missing-client"):
        call_command("import_templates", "--dir", str(tmp_path), "--client", "missing-client")


def test_import_rejects_missing_or_empty_directory(tmp_path, client_record):
    with pytest.raises(CommandError, match="Not a directory"):
        call_command("import_templates", "--dir", str(tmp_path / "nope"), "--client", "dtc-courses")

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(CommandError, match="No .md template files"):
        call_command("import_templates", "--dir", str(empty_dir), "--client", "dtc-courses")
