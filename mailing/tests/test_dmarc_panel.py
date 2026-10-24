import gzip
import io
import json
import os
import tempfile
import zipfile

import pytest
from django.core.management import call_command
from django.test import Client as HttpClient

from mailing.models import DmarcArchive, DmarcRecord, DmarcReport, InboundDomain
from mailing.services.admin_auth import create_admin_api_key
from mailing.tests.test_dmarc import XML, domain, report_mail  # noqa: F401,F811

pytestmark = pytest.mark.django_db


def write_local(tmp_path, name, payload):
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def zip_payload(inner_name="report.xml", body=XML):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(inner_name, body)
    return stream.getvalue()


def other_xml():
    return XML.replace(b"<domain>example.com</domain>", b"<domain>other.com</domain>").replace(
        b"<report_id>ac16-test</report_id>", b"<report_id>other-1</report_id>"
    )


def run_import(*args):
    output = io.StringIO()
    call_command("import_dmarc_reports", *args, stdout=output)
    return json.loads(output.getvalue())


def test_import_local_formats_and_counts(domain, tmp_path):  # noqa: F811
    write_local(tmp_path, "a.xml", XML)
    write_local(tmp_path, "b.xml.gz", gzip.compress(XML.replace(b"ac16-test", b"ac16-gz")))
    write_local(tmp_path, "c.zip", zip_payload(body=XML.replace(b"ac16-test", b"ac16-zip")))
    write_local(tmp_path, "d.eml", report_mail(payload=zip_payload(body=XML.replace(b"ac16-test", b"ac16-eml"))))
    write_local(tmp_path, "ignore.txt", b"not a report")
    stats = run_import(f"--local-dir={tmp_path}")
    assert stats["scanned"] == 4, stats
    assert stats["imported"] == 4, stats
    assert stats["duplicates"] == 0 and not stats["errors"], stats
    assert DmarcReport.objects.count() == 4
    assert DmarcRecord.objects.count() == 4


def test_import_gz_suffix_and_idempotent_rerun(domain, tmp_path):  # noqa: F811
    write_local(tmp_path, "a.gz", gzip.compress(XML))
    first = run_import(f"--local-dir={tmp_path}")
    assert (first["imported"], first["duplicates"]) == (1, 0)
    second = run_import(f"--local-dir={tmp_path}")
    assert (second["imported"], second["duplicates"]) == (0, 1), second
    assert DmarcReport.objects.count() == 1


def test_import_dry_run_writes_nothing(domain, tmp_path):  # noqa: F811
    write_local(tmp_path, "a.xml", XML)
    stats = run_import(f"--local-dir={tmp_path}", "--dry-run")
    assert stats["imported"] == 1 and DmarcReport.objects.count() == 0, stats


def test_import_parse_errors_recorded_not_fatal(domain, tmp_path):  # noqa: F811
    write_local(tmp_path, "bad.xml", b"<broken")
    write_local(tmp_path, "good.xml", XML)
    stats = run_import(f"--local-dir={tmp_path}")
    assert stats["imported"] == 1 and len(stats["errors"]) == 1, stats
    assert DmarcReport.objects.count() == 1


def test_import_unknown_domain_is_error(tmp_path):
    write_local(tmp_path, "a.xml", XML)
    stats = run_import(f"--local-dir={tmp_path}")
    assert len(stats["errors"]) == 1, stats
    assert DmarcReport.objects.count() == 0


def test_import_multi_domain(domain, tmp_path):  # noqa: F811
    other = InboundDomain.objects.create(name="other.com", client=domain.client, from_email="relay@other.com")
    write_local(tmp_path, "a.xml", XML)
    write_local(tmp_path, "b.xml", other_xml())
    stats = run_import(f"--local-dir={tmp_path}")
    assert stats["imported"] == 2, stats
    assert set(DmarcArchive.objects.values_list("domain__name", flat=True)) == {"example.com", "other.com"}
    assert other.pk


def login_staff(django_user_model):
    user = django_user_model.objects.create_user(username="dmarc-ui", is_staff=True)
    http = HttpClient()
    http.force_login(user)
    return http


def seed_reports(domain):  # noqa: F811
    other = InboundDomain.objects.create(name="other.com", client=domain.client, from_email="relay@other.com")
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "a.xml"), "wb") as fh:
            fh.write(XML)
        with open(os.path.join(tmp, "b.xml"), "wb") as fh:
            fh.write(other_xml())
        out = io.StringIO()
        call_command("import_dmarc_reports", f"--local-dir={tmp}", stdout=out)
    return other


def test_list_filters_counts_and_pagination(domain, django_user_model):  # noqa: F811
    seed_reports(domain)
    http = login_staff(django_user_model)
    response = http.get("/dmarc/")
    assert response.status_code == 200
    assert response.context["page"].paginator.count == 2
    response = http.get("/dmarc/?domain=other.com")
    assert response.context["page"].paginator.count == 1
    response = http.get("/dmarc/?org=Reporter")
    assert response.context["page"].paginator.count == 2
    response = http.get("/dmarc/?org=no-such-org")
    assert response.context["page"].paginator.count == 0
    row = http.get("/dmarc/").context["page"].object_list[0]
    assert row.record_count == 1 and row.message_total == 3
    assert HttpClient().get("/dmarc/").status_code == 302


def test_detail_rows_and_downloads(domain, django_user_model):  # noqa: F811
    seed_reports(domain)
    http = login_staff(django_user_model)
    report = DmarcReport.objects.get(report_id="ac16-test")
    response = http.get(f"/dmarc/{report.pk}/")
    assert response.status_code == 200
    assert response.context["total_messages"] == 3
    assert len(response.context["records"]) == 1
    row = response.context["records"][0]
    assert (row.source_ip, row.dmarc_pass, row.header_from) == ("192.0.2.16", True, "example.com")
    assert row.dkim_detail["selector"] == "test"
    xml = http.get(f"/dmarc/{report.pk}/xml/")
    assert xml.status_code == 200 and b"<feedback>" in xml.content
    attachment = report.attachment
    blob = http.get(f"/dmarc/{report.pk}/attachments/{attachment.pk}/")
    assert blob.status_code == 200 and blob.content == bytes(attachment.payload)
    anon = HttpClient()
    assert anon.get(f"/dmarc/{report.pk}/xml/").status_code == 302
    assert anon.get(f"/dmarc/{report.pk}/attachments/{attachment.pk}/").status_code == 302


def test_summary_aggregation_multi_domain(domain, django_user_model):  # noqa: F811
    seed_reports(domain)
    http = login_staff(django_user_model)
    response = http.get("/dmarc/summary/?days=365")
    assert response.status_code == 200
    by_domain = {r["report__attachment__archive__domain__name"]: r for r in response.context["by_domain"]}
    assert set(by_domain) == {"example.com", "other.com"}
    assert by_domain["example.com"]["messages"] == 3
    assert response.context["report_count"] == 2
    assert response.context["over_time"]
    single = http.get("/dmarc/summary/?days=365&domain=other.com")
    assert {r["report__attachment__archive__domain__name"] for r in single.context["by_domain"]} == {"other.com"}


def test_admin_summary_api_auth(domain, django_user_model):  # noqa: F811
    seed_reports(domain)
    user = django_user_model.objects.create_user(username="dmarc-sum", is_staff=True, is_superuser=True)
    _, raw_key = create_admin_api_key(user=user, name="sum")
    http = HttpClient(HTTP_AUTHORIZATION="Bearer " + raw_key)
    response = http.get("/api/admin/inbound-dmarc-summary")
    assert response.status_code == 200
    assert len(response.json()["results"]) == 2
    assert HttpClient().get("/api/admin/inbound-dmarc-summary").status_code in (401, 403)
