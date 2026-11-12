import json
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from django.contrib.auth import get_user_model
from django.test import Client as HttpClient

from mailing.forms import ClientForm
from mailing.models import Client, OperatorAudit, Organization, SendingDomain
from mailing.services.admin_auth import create_admin_api_key
from mailing.services.api import update_client_sender_policy_for_client
from mailing.services.api_errors import ApiValidationError
from mailing.services.auth import create_client_api_key
from mailing.services.sending_domains import refresh
from mailing.ses_routes import route_for_source

pytestmark = pytest.mark.django_db


@pytest.fixture
def domain_setup(monkeypatch):
    org = Organization.objects.create(name="Test", slug="test")
    client = Client.objects.create(name="Monitor", slug="monitor", organization=org)
    row = SendingDomain.objects.create(
        name="dtcdev.click",
        organization=org,
        role_arn="arn:aws:iam::817685572750:role/relay-sandbox-email-send",
        region="us-east-1",
        configuration_set="datamailer-sandbox",
    )
    ses = Mock()
    ses.get_email_identity.return_value = {
        "VerifiedForSendingStatus": True,
        "VerificationStatus": "SUCCESS",
        "DkimAttributes": {"Status": "SUCCESS", "Tokens": ["token"]},
        "MailFromAttributes": {"MailFromDomain": "mail.dtcdev.click", "MailFromDomainStatus": "SUCCESS"},
    }
    monkeypatch.setattr("mailing.services.sending_domains.provider", lambda row: ses)
    monkeypatch.setattr("mailing.services.sending_domains.dmarc_status", lambda name: {"status": "published"})
    return client, row, ses


def test_provider_status_and_persistent_route(domain_setup):
    client, row, ses = domain_setup
    assert refresh(row)["mail_from_domain"] == "mail.dtcdev.click"
    assert route_for_source("DTC Alerts <alerts@dtcdev.click>").region == "us-east-1"
    result = update_client_sender_policy_for_client(
        {"senders": [{"id": "alerts", "email": "DTC Alerts <alerts@dtcdev.click>"}]}, client
    )
    assert result["default_sender_id"] == "alerts"
    ses.get_configuration_set.assert_called_with(ConfigurationSetName="datamailer-sandbox")


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("unverified", "domain_unverified"),
        ("denied", "domain_unrouted"),
        ("other_org", "domain_not_registered"),
        ("restricted", "domain_not_registered"),
    ],
)
def test_sender_rejected_before_save(domain_setup, mode, reason):
    client, row, ses = domain_setup
    if mode == "unverified":
        ses.get_email_identity.return_value["VerifiedForSendingStatus"] = False
    elif mode == "denied":
        ses.get_email_identity.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "private-provider-detail"}}, "GetEmailIdentity"
        )
    elif mode == "other_org":
        client.organization = Organization.objects.create(name="Other", slug="other")
        client.save()
    else:
        row.clients.add(Client.objects.create(name="Other", slug="other", organization=row.organization))
    with pytest.raises(ApiValidationError) as error:
        update_client_sender_policy_for_client({"senders": [{"id": "alerts", "email": "alerts@dtcdev.click"}]}, client)
    assert reason in str(error.value.errors)
    assert "private-provider-detail" not in str(error.value.errors)
    client.refresh_from_db()
    assert client.sender_emails == []


def test_registration_creates_identity_in_provider(domain_setup):
    _, row, ses = domain_setup
    identity = ses.get_email_identity.return_value
    ses.get_email_identity.side_effect = [
        ClientError({"Error": {"Code": "NotFoundException"}}, "GetEmailIdentity"),
        identity,
    ]
    refresh(row, register=True)
    ses.create_email_identity.assert_called_once_with(EmailIdentity="dtcdev.click")
    ses.put_email_identity_mail_from_attributes.assert_called_once_with(
        EmailIdentity="dtcdev.click", MailFromDomain="mail.dtcdev.click", BehaviorOnMxFailure="REJECT_MESSAGE"
    )


def test_domain_admin_and_tenant_read_permissions(domain_setup):
    client, row, _ = domain_setup
    user = get_user_model().objects.create_user("operator", is_staff=True)
    _, raw = create_admin_api_key(user=user, name="test")
    http = HttpClient(HTTP_AUTHORIZATION="Bearer " + raw)
    assert http.get("/api/admin/sending-domains").status_code == 200
    assert http.post(f"/api/admin/sending-domains/{row.pk}/verify").status_code == 200
    response = http.patch(
        f"/api/admin/sending-domains/{row.pk}", json.dumps({"region": 42}), content_type="application/json"
    )
    assert response.status_code == 400
    _, key = create_client_api_key(client=client, name="test")
    tenant = HttpClient(HTTP_AUTHORIZATION="Bearer " + key)
    assert tenant.get("/api/admin/sending-domains").status_code == 401
    assert tenant.get("/api/client/sending-domains").json()["results"][0]["name"] == "dtcdev.click"
    assert tenant.post("/api/client/sending-domains").status_code == 405
    assert http.delete(f"/api/admin/sending-domains/{row.pk}").json()["enabled"] is False
    with pytest.raises(Exception, match="disabled"):
        route_for_source("alerts@dtcdev.click")


def test_staff_sender_form_rejects_unknown_domain(domain_setup):
    client, _, _ = domain_setup
    form = ClientForm(
        {
            "name": client.name,
            "slug": client.slug,
            "organization": client.organization_id,
            "sender_emails": "alerts=alerts@unregistered.example",
            "default_sender_id": "alerts",
        },
        instance=client,
    )
    assert not form.is_valid()
    assert "domain_not_registered" in str(form.errors["sender_emails"])


def test_admin_registration_is_audited_and_tenant_scoped(domain_setup):
    client, _, _ = domain_setup
    user = get_user_model().objects.create_user("registrar", is_staff=True)
    _, raw = create_admin_api_key(user=user, name="register")
    http = HttpClient(HTTP_AUTHORIZATION="Bearer " + raw)
    data = {
        "name": "example.net",
        "organization_id": client.organization_id,
        "client_ids": [client.pk],
        "role_arn": "arn:aws:iam::817685572750:role/test",
        "region": "us-east-1",
        "configuration_set": "test",
    }
    response = http.post("/api/admin/sending-domains", json.dumps(data), content_type="application/json")
    assert response.status_code == 201
    assert response.json()["verification"]["verified"] is True
    assert response.json()["client_ids"] == [client.pk]
    assert OperatorAudit.objects.filter(action="sending_domain.create").exists()
    other = Organization.objects.create(name="Other", slug="other")
    alien = Client.objects.create(organization=other, name="Alien", slug="alien")
    data.update(name="another.net", client_ids=[alien.pk])
    assert http.post("/api/admin/sending-domains", json.dumps(data), content_type="application/json").status_code == 400
    assert not SendingDomain.objects.filter(name="another.net").exists()
