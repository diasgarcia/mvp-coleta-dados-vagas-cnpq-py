from job_collector.sanitize import REDACTED, sanitize, sanitize_text


def test_sanitize_redacts_nested_secret_fields_and_keeps_pagination() -> None:
    original = {
        "api_key": "serp-secret",
        "credentials": {
            "clientSecret": "client-secret",
            "refresh_token": "refresh-secret",
            "secret_key": "another-secret",
        },
        "serpapi_pagination": {"next_page_token": "opaque-page-token"},
        "jobs_results": [{"job_id": "job-1", "title": "Software Engineer"}],
    }

    sanitized = sanitize(original)

    assert sanitized["api_key"] == REDACTED
    assert sanitized["credentials"]["clientSecret"] == REDACTED
    assert sanitized["credentials"]["refresh_token"] == REDACTED
    assert sanitized["credentials"]["secret_key"] == REDACTED
    assert sanitized["serpapi_pagination"]["next_page_token"] == "opaque-page-token"
    assert sanitized["jobs_results"][0]["job_id"] == "job-1"
    assert original["api_key"] == "serp-secret"


def test_sanitize_text_redacts_headers_bearer_and_assignments() -> None:
    text = (
        "Authorization: Bearer auth-secret\n"
        "Cookie=session=cookie-secret\n"
        "Set-Cookie: other=cookie-secret-two\n"
        '{"Authorization":"Basic basic-secret"}\n'
        'fallback Bearer loose-secret password=plain-secret {"client_secret":"json-secret"}'
    )

    sanitized = sanitize_text(text)

    assert sanitized is not None
    assert "auth-secret" not in sanitized
    assert "cookie-secret" not in sanitized
    assert "cookie-secret-two" not in sanitized
    assert "basic-secret" not in sanitized
    assert "loose-secret" not in sanitized
    assert "plain-secret" not in sanitized
    assert "json-secret" not in sanitized
    assert sanitized.count(REDACTED) >= 7
    assert '{"Authorization":"[REDACTED]"}' in sanitized
    assert '{"client_secret":"[REDACTED]"}' in sanitized


def test_sanitize_text_redacts_authenticated_urls_but_keeps_safe_parameters() -> None:
    text = (
        "postgresql://postgres:db-secret@localhost:5433/job_market-py "
        "https://example.test/search?api_key=url-secret&next_page_token=opaque-token"
        "&access_token=access-secret"
    )

    sanitized = sanitize_text(text)

    assert sanitized is not None
    assert "db-secret" not in sanitized
    assert "url-secret" not in sanitized
    assert "access-secret" not in sanitized
    assert "next_page_token=opaque-token" in sanitized


def test_sanitize_handles_lists_and_encoded_secret_parameters() -> None:
    value = [
        "https%3A%2F%2Fexample.test%3Fapi_key%3Dencoded-secret%2Fpart%26page%3D2",
        {"company": "Example", "authorization": "Basic basic-secret"},
        None,
    ]

    sanitized = sanitize(value)

    assert "encoded-secret" not in sanitized[0]
    assert "%2Fpart" not in sanitized[0]
    assert sanitized[1] == {"company": "Example", "authorization": REDACTED}
    assert sanitized[2] is None
