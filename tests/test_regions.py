from copy import deepcopy

import pytest

from job_collector.regions import REGIONS, build_monthly_plan, validate_regions


def test_catalog_has_eight_valid_unique_regions() -> None:
    validate_regions()

    assert len(REGIONS) == 8
    assert {region["key"] for region in REGIONS} == {
        "sao-paulo",
        "campinas",
        "sao-jose-dos-campos",
        "sorocaba",
        "ribeirao-preto",
        "santos",
        "bauru",
        "sao-jose-do-rio-preto",
    }
    assert len({region["theirstack_location_id"] for region in REGIONS}) == 8
    assert len({region["serpapi_location"] for region in REGIONS}) == 8


@pytest.mark.parametrize(
    ("field", "invalid_value", "message"),
    [
        ("theirstack_location_id", None, "ID TheirStack inválido"),
        ("serpapi_location", "", "Localização canônica SerpApi inválida"),
    ],
)
def test_catalog_rejects_missing_provider_location(
    field: str, invalid_value: object, message: str
) -> None:
    regions = deepcopy(REGIONS)
    regions[0][field] = invalid_value

    with pytest.raises(ValueError, match=message):
        validate_regions(regions)


def test_plan_has_eight_theirstack_requests_with_safe_limits() -> None:
    plan = build_monthly_plan("2026-07")

    assert len(plan["theirstack"]) == 8
    assert {item["limit"] for item in plan["theirstack"]} == {10}
    assert {item["max_pages"] for item in plan["theirstack"]} == {1}
    assert plan["limits"]["theirstack_requested_items"] == 80


def test_plan_has_two_serpapi_queries_per_region() -> None:
    plan = build_monthly_plan("2026-07")

    assert len(plan["serpapi"]) == 16
    assert plan["limits"]["serpapi_searches"] == 16
    for region in REGIONS:
        queries = {
            item["query"] for item in plan["serpapi"] if item["sample_region"] == region["key"]
        }
        assert queries == {"software engineer", "desenvolvedor de software"}


@pytest.mark.parametrize(
    ("budgets", "message"),
    [
        ({"theirstack_budget": 79}, "Orçamento TheirStack insuficiente"),
        ({"serpapi_budget": 15}, "Orçamento SerpApi insuficiente"),
    ],
)
def test_plan_rejects_budget_below_planned_use(budgets: dict[str, int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_monthly_plan("2026-07", **budgets)


def test_plan_records_monthly_round_and_zero_retries() -> None:
    plan = build_monthly_plan("2026-07")

    assert plan["round_id"] == "2026-07"
    assert plan["collection_kind"] == "monthly"
    assert plan["limits"] == {
        "theirstack_budget": 80,
        "theirstack_requested_items": 80,
        "serpapi_budget": 16,
        "serpapi_searches": 16,
        "max_pages": 1,
        "max_retries": 0,
    }
