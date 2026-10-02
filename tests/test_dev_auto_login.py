import pytest
from django.contrib.auth import get_user_model

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_user():
    return get_user_model().objects.create_superuser("admin", "admin@example.com", "admin")


def test_debug_pre_authenticates_requests_as_superuser(client, settings, admin_user):
    settings.DEBUG = True
    response = client.get("/")
    assert response.status_code == 200
    assert response.context["user"] == admin_user


def test_without_debug_the_operator_ui_still_requires_login(client, admin_user):
    # The test suite runs with DEBUG off, like a deployed environment: the
    # middleware must stay dormant there and the staff gate must hold.
    response = client.get("/")
    assert response.status_code == 302
    assert response["Location"] == "/admin/login/?next=/"
