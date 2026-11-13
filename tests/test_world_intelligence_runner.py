import pytest

from aitos.world_intelligence.runner import build_source_adapters


def test_runner_builds_explicit_rss_and_gdelt_sources():
    adapters = build_source_adapters(
        [
            {
                "type": "rss",
                "source_id": "exchange-news",
                "feed_url": "https://exchange.example/rss",
                "allowed_hosts": ["exchange.example"],
            },
            {"type": "gdelt", "query": "bitcoin exchange"},
        ],
        environ={},
    )
    assert [adapter.source_id for adapter in adapters] == ["exchange-news", "gdelt-doc"]


def test_runner_resolves_fred_key_from_named_environment_variable():
    adapters = build_source_adapters(
        [
            {
                "type": "fred",
                "series_id": "CPIAUCSL",
                "start_date": "2026-01-01",
                "api_key_env": "WIATS_TEST_FRED_KEY",
            }
        ],
        environ={"WIATS_TEST_FRED_KEY": "secret"},
    )
    assert adapters[0].source_id == "fred-cpiaucsl"


def test_runner_requires_operator_secrets_and_unique_sources():
    with pytest.raises(ValueError, match="environment variable"):
        build_source_adapters(
            [
                {
                    "type": "fred",
                    "series_id": "GDP",
                    "start_date": "2026-01-01",
                    "api_key_env": "MISSING",
                }
            ],
            environ={},
        )
    with pytest.raises(ValueError, match="unique"):
        build_source_adapters(
            [
                {
                    "type": "rss",
                    "source_id": "same",
                    "feed_url": "https://news.example/rss",
                    "allowed_hosts": ["news.example"],
                },
                {
                    "type": "rss",
                    "source_id": "same",
                    "feed_url": "https://news.example/other",
                    "allowed_hosts": ["news.example"],
                },
            ],
            environ={},
        )
