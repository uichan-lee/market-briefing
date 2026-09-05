"""Tests for config loading and validation.

The committed config files are loaded as-is, and the hand-editing failure modes
are exercised against temporary files.
"""

from __future__ import annotations

import pytest
import yaml

from src.util.config import (
    CONFIG_DIR,
    ConfigError,
    load_aliases,
    load_all,
    load_delivery,
    load_filing_ids,
    load_models,
    load_sector_mapping,
    load_watchlist,
)


def write(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# --- the committed files actually parse ----------------------------------


def test_all_committed_config_files_load():
    loaded = load_all()
    assert set(loaded) == {
        "watchlist",
        "aliases",
        "delivery",
        "models",
        "sector_mapping",
        "rating",
        "news_feeds",
        "filing_ids",
    }


def test_committed_watchlist_has_the_two_required_large_caps():
    tickers = {e.ticker for e in load_watchlist()}
    assert {"005930", "000660"} <= tickers


def test_committed_aliases_match_the_seeded_watchlist_entries():
    aliases = load_aliases()
    assert aliases["005930"].canonical == "삼성전자"
    assert "하이닉스" in aliases["000660"].aliases
    # Preferred shares are a separate ticker and must be excluded, not aliased.
    assert "삼성전자우" in aliases["005930"].exclude
    assert "삼성전자우" not in aliases["005930"].aliases


def test_committed_aliases_keep_group_names_ambiguous():
    """A bare group name must match nothing, per SPEC §4.2."""
    aliases = load_aliases()
    assert "삼성" in aliases["005930"].ambiguous_parents
    assert "삼성" not in aliases["005930"].aliases


# --- the YAML octal trap -------------------------------------------------


def test_unquoted_ticker_is_rejected_with_an_explanation(tmp_path):
    """000660 unquoted parses as 432. This must fail loudly, not load."""
    path = write(tmp_path, "watchlist.yaml", "kr:\n  - ticker: 000660\n    name: SK하이닉스\n")
    with pytest.raises(ConfigError, match="unquoted"):
        load_watchlist(path)


def test_unquoted_ticker_is_rejected_in_aliases_too(tmp_path):
    path = write(
        tmp_path,
        "aliases.yaml",
        "000660:\n  canonical: SK하이닉스\n  aliases: [하이닉스]\n",
    )
    with pytest.raises(ConfigError, match="unquoted"):
        load_aliases(path)


def test_a_malformed_ticker_string_is_rejected(tmp_path):
    path = write(tmp_path, "watchlist.yaml", 'kr:\n  - ticker: "00660"\n    name: x\n')
    with pytest.raises(ConfigError, match="6-digit"):
        load_watchlist(path)


# --- alias collisions ----------------------------------------------------


def test_an_alias_claimed_by_two_tickers_is_a_hard_error(tmp_path):
    path = write(
        tmp_path,
        "aliases.yaml",
        '"005930":\n'
        "  canonical: 삼성전자\n"
        "  aliases: [삼성전자, 반도체]\n"
        '"000660":\n'
        "  canonical: SK하이닉스\n"
        "  aliases: [SK하이닉스, 반도체]\n",
    )
    with pytest.raises(ConfigError, match="claimed by both"):
        load_aliases(path)


def test_an_alias_that_is_also_excluded_is_rejected(tmp_path):
    path = write(
        tmp_path,
        "aliases.yaml",
        '"005930":\n'
        "  canonical: 삼성전자\n"
        "  aliases: [삼성전자, 삼성전자우]\n"
        "  exclude: [삼성전자우]\n",
    )
    with pytest.raises(ConfigError, match="both aliases and exclude"):
        load_aliases(path)


def test_an_entry_with_no_aliases_is_rejected(tmp_path):
    path = write(tmp_path, "aliases.yaml", '"005930":\n  canonical: 삼성전자\n  aliases: []\n')
    with pytest.raises(ConfigError, match="no aliases"):
        load_aliases(path)


def test_the_same_alias_repeated_under_one_ticker_is_fine(tmp_path):
    """Only cross-ticker collisions are corrupting."""
    path = write(
        tmp_path,
        "aliases.yaml",
        '"005930":\n  canonical: 삼성전자\n  aliases: [삼성전자, 삼성전자]\n',
    )
    assert load_aliases(path)["005930"].canonical == "삼성전자"


# --- watchlist ------------------------------------------------------------


def test_duplicate_watchlist_tickers_are_rejected(tmp_path):
    path = write(
        tmp_path,
        "watchlist.yaml",
        'kr:\n  - ticker: "005930"\n    name: a\n  - ticker: "005930"\n    name: b\n',
    )
    with pytest.raises(ConfigError, match="listed twice"):
        load_watchlist(path)


def test_a_watchlist_entry_missing_a_name_is_rejected(tmp_path):
    path = write(tmp_path, "watchlist.yaml", 'kr:\n  - ticker: "005930"\n')
    with pytest.raises(ConfigError, match="missing"):
        load_watchlist(path)


def test_an_empty_watchlist_loads(tmp_path):
    """Its committed starting state; filling it is Ricky's task, not a load error."""
    assert load_watchlist(write(tmp_path, "watchlist.yaml", "kr: []\nus: []\n")) == []


def test_held_defaults_to_false(tmp_path):
    path = write(tmp_path, "watchlist.yaml", 'kr:\n  - ticker: "005930"\n    name: 삼성전자\n')
    assert load_watchlist(path)[0].held is False


# --- watchlist: the US section --------------------------------------------


def _both(tmp_path):
    return write(
        tmp_path,
        "watchlist.yaml",
        'kr:\n  - ticker: "005930"\n    name: 삼성전자\nus:\n  - ticker: "AAPL"\n    name: Apple\n',
    )


def test_both_market_sections_are_read(tmp_path):
    # The regression this guards: the loader read only `kr`, so US entries were
    # accepted by YAML and then silently dropped — the exact quiet failure this
    # project treats as its worst outcome.
    entries = load_watchlist(_both(tmp_path))
    assert {(e.ticker, e.market) for e in entries} == {("005930", "KR"), ("AAPL", "US")}


def test_market_filters_to_one_section(tmp_path):
    path = _both(tmp_path)
    assert [e.ticker for e in load_watchlist(path, market="KR")] == ["005930"]
    assert [e.ticker for e in load_watchlist(path, market="US")] == ["AAPL"]


def test_an_unknown_market_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="market must be"):
        load_watchlist(_both(tmp_path), market="JP")


def test_an_unquoted_us_ticker_parsed_as_a_boolean_is_rejected(tmp_path):
    # YAML 1.1 reads bare ON as True. This is the US-side counterpart of the
    # octal trap, and it fails identically: a ticker that is no longer a string.
    path = write(tmp_path, "watchlist.yaml", "us:\n  - ticker: ON\n    name: ON Semiconductor\n")
    with pytest.raises(ConfigError, match="parsed as a boolean"):
        load_watchlist(path)


def test_a_lowercase_us_ticker_is_rejected(tmp_path):
    path = write(tmp_path, "watchlist.yaml", 'us:\n  - ticker: "aapl"\n    name: Apple\n')
    with pytest.raises(ConfigError, match="not a US symbol"):
        load_watchlist(path)


def test_a_us_ticker_may_carry_a_dot_or_dash(tmp_path):
    # Class-share symbols such as BRK.B and BF-B are legitimate.
    path = write(tmp_path, "watchlist.yaml", 'us:\n  - ticker: "BRK.B"\n    name: Berkshire\n')
    assert load_watchlist(path)[0].ticker == "BRK.B"


def test_the_same_symbol_in_both_markets_does_not_collide(tmp_path):
    # Tickers are namespaced per market, so a six-digit KR code and a US symbol
    # can never conflict — but duplicates *within* one market still must.
    path = write(
        tmp_path,
        "watchlist.yaml",
        'us:\n  - ticker: "AAPL"\n    name: a\n  - ticker: "AAPL"\n    name: b\n',
    )
    with pytest.raises(ConfigError, match="US ticker AAPL listed twice"):
        load_watchlist(path)


def test_the_committed_watchlist_holds_both_markets(tmp_path):
    kr = load_watchlist(market="KR")
    us = load_watchlist(market="US")
    assert len(kr) >= 15, "MANUAL-TASKS.md §2 asks for at least 15 Korean tickers"
    assert us, "the US section is populated; SPEC §2.2① reads across from it"
    # Every US symbol was verified against Tiingo when the file was written.
    assert {"NVDA", "MU", "AAPL"} <= {e.ticker for e in us}


# --- delivery -------------------------------------------------------------


def test_an_undeclared_delivery_channel_is_rejected(tmp_path):
    """CLAUDE.md forbids a channel not in delivery.yaml; catch it at load."""
    path = write(tmp_path, "delivery.yaml", "channels:\n  - type: telegram\n")
    with pytest.raises(ConfigError, match="unknown channel type"):
        load_delivery(path)


def test_committed_delivery_declares_vault_and_email():
    types = {c["type"] for c in load_delivery()["channels"]}
    assert types == {"vault", "email"}


# --- models ---------------------------------------------------------------


def test_a_stage_missing_its_provider_is_rejected(tmp_path):
    path = write(
        tmp_path,
        "models.yaml",
        "embedding:\n  provider: local\n  model: x\n"
        "scoring:\n  model: y\n"
        "synthesis:\n  provider: anthropic\n  model: z\n",
    )
    with pytest.raises(ConfigError, match="missing"):
        load_models(path)


def test_a_non_boolean_stage_enabled_flag_is_rejected(tmp_path):
    path = write(
        tmp_path,
        "models.yaml",
        "embedding:\n  provider: local\n  model: x\n"
        "scoring:\n  provider: openai\n  model: y\n"
        "synthesis:\n  provider: anthropic\n  model: z\n  enabled: disabled\n",
    )
    with pytest.raises(ConfigError, match="non-boolean"):
        load_models(path)


def test_committed_models_names_all_three_stages():
    models = load_models()
    assert {"embedding", "scoring", "synthesis"} <= models.keys()
    assert models["synthesis"]["enabled"] is False
    # None means "send no temperature at all" (config/models.yaml's own
    # convention) rather than "send zero". gpt-5.4 (2026-08-13, superseding
    # gpt-5.1) rejects an explicit 0 the same way every gpt-5.x-and-later
    # OpenAI model tested so far has.
    assert models["scoring"]["temperature"] is None


# --- sector mapping -------------------------------------------------------


def test_committed_sector_mapping_includes_the_semiconductor_link():
    mappings = {m["us"]: m["kr_sector"] for m in load_sector_mapping()}
    assert mappings["SMH"] == "반도체"


def test_a_mapping_missing_its_korean_side_is_rejected(tmp_path):
    path = write(tmp_path, "sector_mapping.yaml", "mappings:\n  - us: SMH\n")
    with pytest.raises(ConfigError, match="missing"):
        load_sector_mapping(path)


def test_a_symbol_no_collector_fetches_is_rejected(tmp_path):
    """The failure this prevents is silent: SPEC §2.2① renders one row per
    mapping, so an uncollected symbol produces an empty correlation that reads
    as 'the link broke down' rather than 'we never had the data'. The committed
    file carried RUT — the index, not the IWM ETF that is actually fetched —
    until 2026-08-06."""
    path = write(
        tmp_path,
        "sector_mapping.yaml",
        'mappings:\n  - us: RUT\n    kr_sector: 코스닥\n    tickers: ["005930"]\n',
    )
    with pytest.raises(ConfigError, match="not collected"):
        load_sector_mapping(path)


def test_a_mapping_naming_a_ticker_outside_the_watchlist_is_rejected(tmp_path):
    """The failure this prevents is the one found on 2026-08-06: the Korean side
    matched nothing, so the section rendered '이력 부족' — indistinguishable from
    a correlation that genuinely broke down."""
    path = write(
        tmp_path,
        "sector_mapping.yaml",
        'mappings:\n  - us: SMH\n    kr_sector: 반도체\n    tickers: ["999999"]\n',
    )
    with pytest.raises(ConfigError, match="not in watchlist"):
        load_sector_mapping(path)


def test_a_mapping_with_no_tickers_is_rejected(tmp_path):
    path = write(tmp_path, "sector_mapping.yaml", "mappings:\n  - us: SMH\n    kr_sector: 반도체\n")
    with pytest.raises(ConfigError, match="missing"):
        load_sector_mapping(path)


def test_every_committed_mapping_names_a_collected_symbol():
    from src.collectors.us_price import INDEX_ETFS

    assert [m["us"] for m in load_sector_mapping() if m["us"] not in INDEX_ETFS] == []


# --- filing ids -------------------------------------------------------


def test_committed_filing_ids_covers_the_whole_watchlist():
    load_filing_ids()  # raises if any watchlist ticker has no entry


def _committed_filing_ids() -> dict:
    return yaml.safe_load((CONFIG_DIR / "filing_ids.yaml").read_text(encoding="utf-8"))


def test_a_watchlist_ticker_missing_from_filing_ids_is_rejected(tmp_path):
    """The failure this guards: a ticker with no entry would make the filings
    collectors silently skip it forever, with nothing in the report header to
    say so — so this is a hard error, not a warning, unlike aliases.yaml's
    missing-alias tolerance."""
    raw = _committed_filing_ids()
    dropped = next(iter(raw["kr"]))
    del raw["kr"][dropped]
    path = tmp_path / "filing_ids.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match=dropped):
        load_filing_ids(path)


def test_a_malformed_filing_id_value_is_rejected(tmp_path):
    raw = _committed_filing_ids()
    some_ticker = next(iter(raw["kr"]))
    raw["kr"][some_ticker] = {}
    path = tmp_path / "filing_ids.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="corp_code"):
        load_filing_ids(path)


# --- missing files --------------------------------------------------------


def test_a_missing_file_raises_rather_than_returning_empty(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        load_watchlist(tmp_path / "nope.yaml")
