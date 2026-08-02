"""Tests for config loading and validation.

The committed config files are loaded as-is, and the hand-editing failure modes
are exercised against temporary files.
"""

from __future__ import annotations

import pytest

from src.util.config import (
    ConfigError,
    load_aliases,
    load_all,
    load_delivery,
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
    assert set(loaded) == {"watchlist", "aliases", "delivery", "models", "sector_mapping"}


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


def test_committed_models_names_all_three_stages():
    models = load_models()
    assert {"embedding", "scoring", "synthesis"} <= models.keys()
    assert models["scoring"]["temperature"] == 0


# --- sector mapping -------------------------------------------------------


def test_committed_sector_mapping_includes_the_semiconductor_link():
    mappings = {m["us"]: m["kr_sector"] for m in load_sector_mapping()}
    assert mappings["SMH"] == "반도체"


def test_a_mapping_missing_its_korean_side_is_rejected(tmp_path):
    path = write(tmp_path, "sector_mapping.yaml", "mappings:\n  - us: SMH\n")
    with pytest.raises(ConfigError, match="missing"):
        load_sector_mapping(path)


# --- missing files --------------------------------------------------------


def test_a_missing_file_raises_rather_than_returning_empty(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        load_watchlist(tmp_path / "nope.yaml")
