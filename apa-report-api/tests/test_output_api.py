import io
from urllib.parse import unquote

import docx
import pytest

from tests.conftest import BUCKET
from tests.helpers import complete
from tests.test_reports_api import API, assert_problem


async def test_references_not_ready(client, headers, create_report):
    report = await create_report()
    assert_problem(await client.get(f"{API}/reports/{report['id']}/references", headers=headers), 409, "REPORT_NOT_READY")


async def test_references_for_completed_report(app, client, headers, create_report):
    report = await create_report()
    await complete(app, report["id"])
    resp = await client.get(f"{API}/reports/{report['id']}/references", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [r["id"] for r in items] == ["W1", "W2"]
    first = items[0]
    assert first["inTextCitation"] == "(Smith & Lee, 2021)"
    assert first["authors"] == [{"family": "Smith", "given": "J."}, {"family": "Lee", "given": "K."}]
    assert first["doi"] == "https://doi.org/10.1/abc"
    assert first["isOpenAccess"] is True
    second = items[1]
    assert second["volume"] is None and second["doi"] is None
    assert second["authors"][2] == {"family": "Chen", "given": None}


async def test_export_not_ready(client, headers, create_report):
    report = await create_report()
    assert_problem(await client.get(f"{API}/reports/{report['id']}/export?format=pdf", headers=headers), 409, "REPORT_NOT_READY")


@pytest.mark.parametrize("query", ["", "?format=txt", "?format="])
async def test_export_format_validation(client, headers, create_report, query):
    report = await create_report()
    problem = assert_problem(await client.get(f"{API}/reports/{report['id']}/export{query}", headers=headers), 400)
    assert problem["errors"][0]["field"] == "format"


async def test_export_docx_generates_caches_and_streams(app, client, headers, create_report, aws):
    report = await create_report()
    await complete(app, report["id"])
    resp = await client.get(f"{API}/reports/{report['id']}/export", params={"format": "docx"}, headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="Social Media Use and Adolescent Anxiety- A Review.docx"') or (
        "attachment;" in disposition and ".docx" in disposition
    )
    assert unquote(disposition.split("filename*=UTF-8''")[1]) == "Social Media Use and Adolescent Anxiety  A Review.docx".replace("  ", " ")
    document = docx.Document(io.BytesIO(resp.content))
    text = "\n".join(p.text for p in document.paragraphs)
    assert "Abstract" in text and "References" in text
    assert "Smith, J., & Lee, K. (2021)." in text
    assert "(Zhang et al., 2020)" in text
    key = f"exports/{report['id']}/1.docx"
    cached = aws["s3"].get_object(Bucket=BUCKET, Key=key)["Body"].read()
    assert cached == resp.content
    # Second request is served from S3, byte-identical.
    again = await client.get(f"{API}/reports/{report['id']}/export", params={"format": "docx"}, headers=headers)
    assert again.content == resp.content


async def test_export_pdf(app, client, headers, create_report, aws):
    report = await create_report()
    await complete(app, report["id"])
    resp = await client.get(f"{API}/reports/{report['id']}/export", params={"format": "pdf"}, headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")
    assert int(resp.headers["content-length"]) == len(resp.content)
    assert aws["s3"].head_object(Bucket=BUCKET, Key=f"exports/{report['id']}/1.pdf")["ContentLength"] == len(resp.content)
