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
    def __init__(self, account=None, error=None, identities=None, identities_error=None):
        self.account, self.error = account, error
        self.identities, self.identities_error = identities or [], identities_error

    def list_email_identities(self, **kwargs):
        if self.identities_error:
            raise ClientError({"Error": {"Code": self.identities_error}}, "ListEmailIdentities")
        return {"EmailIdentities": self.identities}

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
        if service == "sts":
            return type("Sts", (), {"get_caller_identity": lambda self: {"Account": "387546586013"}})()
        calls.append((service, role_arn, region_name))
        return by_region[(role_arn, region_name)]

    monkeypatch.setattr(ses_quota, "aws_client", fake)
    return calls


IDENTITIES = [
    {"IdentityName": "b@x.io", "IdentityType": "EMAIL_ADDRESS", "VerificationStatus": "SUCCESS", "SendingEnabled": True},
    {"IdentityName": "x.io", "IdentityType": "DOMAIN", "VerificationStatus": "SUCCESS", "SendingEnabled": True},
]


@override_settings(
    AWS_SES_REGION="eu-west-1",
    RELAY_SES_DOMAIN_ROUTES=ROUTES,
    RELAY_TASK_ROLE_ARNS={},
    RELAY_SES_QUOTA_REGIONS=["eu-west-1", "us-east-1"],
)
def test_rows_cover_every_account_and_region(monkeypatch):
    sandbox = {"SendQuota": {"Max24HourSend": 200.0, "MaxSendRate": 1.0, "SentLast24Hours": 0.0}}
    patch_clients(
        monkeypatch,
        {
            (None, "eu-west-1"): FakeSes(ACCOUNT, identities=IDENTITIES),
            (None, "us-east-1"): FakeSes(sandbox, identities_error="AccessDenied"),
            (ROLE, "eu-west-1"): FakeSes(sandbox),
            (ROLE, "us-east-1"): FakeSes(ACCOUNT),
        },
    )
    rows = ses_quota.ses_quota_rows()
    assert [(r["account"], r["region"], r["mode"]) for r in rows] == [
        ("387546586013", "eu-west-1", "production"),
        ("387546586013", "us-east-1", "SES sandbox"),
        ("111111111111", "eu-west-1", "SES sandbox"),
        ("111111111111", "us-east-1", "production"),
    ]
    assert [(i["name"], i["type"]) for i in rows[0]["identities"]] == [("x.io", "domain"), ("b@x.io", "email")]
    assert rows[1]["identities_status"] == "no_permission"
    assert rows[0]["percent_used"] == 1.0
    assert rows[0]["production_access"] is True
    assert rows[0]["enforcement_status"] == "HEALTHY"
    assert rows[0]["configured_max_send_rate"] == 10.0


@override_settings(AWS_SES_REGION="eu-west-1", RELAY_SES_DOMAIN_ROUTES="", RELAY_TASK_ROLE_ARNS={}, RELAY_SES_QUOTA_REGIONS=[])
def test_permission_denied_is_a_row_not_an_error(monkeypatch):
    patch_clients(monkeypatch, {(None, "eu-west-1"): FakeSes(error="AccessDenied")})
    (row,) = ses_quota.ses_quota_rows()
    assert row["status"] == "no_permission"
    assert "ses:GetAccount" in row["required_iam_actions"]


@override_settings(AWS_SES_REGION="eu-west-1", RELAY_SES_DOMAIN_ROUTES="", RELAY_TASK_ROLE_ARNS={}, RELAY_SES_QUOTA_REGIONS=[])
def test_results_are_cached(monkeypatch):
    calls = patch_clients(monkeypatch, {(None, "eu-west-1"): FakeSes(ACCOUNT)})
    ses_quota.ses_quota_rows()
    ses_quota.ses_quota_rows()
    assert len(calls) == 1
    ses_quota.ses_quota_rows(refresh=True)
    assert len(calls) == 2


@override_settings(AWS_SES_REGION="eu-west-1", RELAY_SES_DOMAIN_ROUTES="", RELAY_TASK_ROLE_ARNS={}, RELAY_SES_QUOTA_REGIONS=[])
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
