from job_collector.sanitize import REDACTED, sanitize, sanitize_text


def test_sanitize_redacts_secrets_without_losing_payload_fields() -> None:
    original = {
        "api_key": "provider-secret",
        "credentials": {"clientSecret": "client-secret"},
        "pagination": {"next_page_token": "opaque-token"},
        "jobs": [{"id": "job-1", "unknown": True}],
        "note": "contains-known-secret",
    }

    clean = sanitize(original, ("known-secret",))

    assert clean["api_key"] == REDACTED
    assert clean["credentials"]["clientSecret"] == REDACTED
    assert clean["pagination"]["next_page_token"] == "opaque-token"
    assert clean["jobs"] == [{"id": "job-1", "unknown": True}]
    assert clean["note"] == f"contains-{REDACTED}"
    assert original["api_key"] == "provider-secret"


def test_sanitize_text_covers_headers_bearer_urls_and_encoded_parameters() -> None:
    text = (
        "Authorization: Bearer auth-secret\nCookie=session-secret\n"
        "postgresql://postgres:db-secret@localhost:5433/job_market-py "
        "https://example.test/?api_key=url-secret&next_page_token=opaque "
        "https%3A%2F%2Fexample.test%3Faccess_token%3Dencoded-secret%26page%3D2"
    )

    clean = sanitize_text(text)

    assert clean is not None
    for secret in ("auth-secret", "session-secret", "db-secret", "url-secret", "encoded-secret"):
        assert secret not in clean
    assert "next_page_token=opaque" in clean
    assert clean.count(REDACTED) >= 5
