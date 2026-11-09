import pytest
from botocore.exceptions import ClientError
from django.contrib.auth import get_user_model
from django.test import Client as HttpClient
from django.test import override_settings

from mailing.services import ses_quota
from mailing.services.admin_auth import create_admin_api_key

pytestmark = pytest.mark.django_db

ROLE = "arn:aws:iam::111111111111:role/sandbox-send"
ROUTES = f"sandbox.example role={ROLE} region=us-east-1 configuration_set=cs"


class FakeSes:
    def __init__(self, account=None, error=None):
        self.account, self.error = account, error

    def get_account(self):
        if self.error:
            raise ClientError({"Error": {"Code": self.error}}, "GetAccount")
        return self.account


ACCOUNT = {
    "SendQuota": {"Max24HourSend": 150000.0, "MaxSendRate": 50.0, "SentLast24Hours": 1500.0},
    "ProductionAccessEnabled": True,
    "EnforcementStatus": "HEALTHY",
}


@pytest.fixture(autouse=True)
def fresh_cache():
    ses_quota._cache.update(rows=None, expires=0.0)


def patch_clients(monkeypatch, by_region):
    calls = []

    def fake(service, *, role_arn=None, region_name=None, **kwargs):
        calls.append((service, role_arn, region_name))
        return by_region[(role_arn, region_name)]

    monkeypatch.setattr(ses_quota, "aws_client", fake)
    return calls


@override_settings(AWS_SES_REGION="eu-west-1", RELAY_SES_DOMAIN_ROUTES=ROUTES, RELAY_TASK_ROLE_ARNS={})
def test_rows_cover_default_and_routed_pairs(monkeypatch):
    patch_clients(monkeypatch, {(None, "eu-west-1"): FakeSes(ACCOUNT), (ROLE, "us-east-1"): FakeSes(ACCOUNT)})
    rows = ses_quota.ses_quota_rows()
    assert [(r["account"], r["region"]) for r in rows] == [
        ("runtime credentials", "eu-west-1"),
        ("111111111111", "us-east-1"),
    ]
    assert rows[0]["percent_used"] == 1.0
    assert rows[0]["production_access"] is True
    assert rows[0]["enforcement_status"] == "HEALTHY"
    assert rows[0]["configured_max_send_rate"] == 10.0


@override_settings(AWS_SES_REGION="eu-west-1", RELAY_SES_DOMAIN_ROUTES="", RELAY_TASK_ROLE_ARNS={})
def test_permission_denied_is_a_row_not_an_error(monkeypatch):
    patch_clients(monkeypatch, {(None, "eu-west-1"): FakeSes(error="AccessDenied")})
    (row,) = ses_quota.ses_quota_rows()
    assert row["status"] == "no_permission"
    assert "ses:GetAccount" in row["required_iam_actions"]


@override_settings(AWS_SES_REGION="eu-west-1", RELAY_SES_DOMAIN_ROUTES="", RELAY_TASK_ROLE_ARNS={})
def test_results_are_cached(monkeypatch):
    calls = patch_clients(monkeypatch, {(None, "eu-west-1"): FakeSes(ACCOUNT)})
    ses_quota.ses_quota_rows()
    ses_quota.ses_quota_rows()
    assert len(calls) == 1
    ses_quota.ses_quota_rows(refresh=True)
    assert len(calls) == 2


@override_settings(AWS_SES_REGION="eu-west-1", RELAY_SES_DOMAIN_ROUTES="", RELAY_TASK_ROLE_ARNS={})
def test_staff_page_and_admin_api(monkeypatch):
    patch_clients(monkeypatch, {(None, "eu-west-1"): FakeSes(ACCOUNT)})
    user = get_user_model().objects.create_user("quota", is_staff=True)
    _, token = create_admin_api_key(user=user, name="quota")
    assert HttpClient().get("/ses-quota/").status_code == 302
    http = HttpClient()
    http.force_login(user)
    page = http.get("/ses-quota/")
    assert page.status_code == 200
    assert "eu-west-1" in page.content.decode()
    assert HttpClient().get("/api/admin/ses-quota").status_code == 401
    response = HttpClient(HTTP_AUTHORIZATION=f"Bearer {token}").get("/api/admin/ses-quota")
    assert response.status_code == 200
    assert response.json()["results"][0]["sent_last_24h"] == 1500.0
