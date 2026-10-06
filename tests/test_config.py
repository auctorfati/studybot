import os
from pathlib import Path

import pytest

from studybot.config import ConfigError, insecure_permissions, load_config
from studybot.__main__ import main

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


def _write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    os.chmod(p, 0o600)
    return p


def test_example_loads(tmp_path):
    p = _write(tmp_path, EXAMPLE.read_text(encoding="utf-8"))
    cfg = load_config(p)
    assert cfg.paths.db_file == tmp_path / "data" / "studybot.sqlite3"
    assert cfg.calendar().boundary.hour == 3
    assert not cfg.llm.configured
    with pytest.raises(ConfigError):
        cfg.require_telegram()


def test_example_moonshot_tiers(tmp_path):
    cfg = load_config(_write(tmp_path, EXAMPLE.read_text(encoding="utf-8"))).llm
    assert (cfg.cheap_model, cfg.flagship_model) == ("kimi-k2.6", "kimi-k3")
    assert cfg.cheap_extra == {"thinking": {"type": "disabled"}}
    assert cfg.flagship_extra == {"reasoning_effort": "low"}
    assert (cfg.max_tokens, cfg.flagship_max_tokens, cfg.flagship_timeout_sec) == (1500, 8000, 120.0)
    assert cfg.prices.flagship_output == 15.0


def test_extra_must_be_table_without_reserved_keys(tmp_path):
    with pytest.raises(ConfigError, match="таблицей"):
        load_config(_write(tmp_path, '[llm]\ncheap_extra = "x"\n'))
    with pytest.raises(ConfigError, match="max_tokens"):
        load_config(_write(tmp_path, '[llm.flagship_extra]\nmax_tokens = 9\n'))


def test_bad_timezone(tmp_path):
    p = _write(tmp_path, '[time]\ntimezone = "Mars/Olympus"\n')
    with pytest.raises(ConfigError):
        load_config(p)


def test_bad_owner(tmp_path):
    p = _write(tmp_path, '[telegram]\nowner_id = "alex"\n')
    with pytest.raises(ConfigError):
        load_config(p)


def test_unknown_price_key(tmp_path):
    p = _write(tmp_path, "[llm.prices]\ncheap_inptu = 1.0\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_permissions(tmp_path):
    p = _write(tmp_path, "")
    assert not insecure_permissions(p)
    os.chmod(p, 0o644)
    assert insecure_permissions(p)


def test_cli_init_db_and_settings(tmp_path, capsys):
    p = _write(tmp_path, EXAMPLE.read_text(encoding="utf-8"))
    assert main(["--config", str(p), "init-db"]) == 0
    assert "0001_init.sql" in capsys.readouterr().out
    assert main(["--config", str(p), "init-db"]) == 0
    assert "ничего" in capsys.readouterr().out
    assert main(["--config", str(p), "settings"]) == 0
    assert "notify_time = '20:30'" in capsys.readouterr().out
    assert main(["--config", str(p), "check"]) == 0
