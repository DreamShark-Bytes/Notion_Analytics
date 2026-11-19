import logging

from sync import _load_token


def test_token_comes_from_the_secrets_file_next_to_the_config(tmp_path):
    (tmp_path / "config_secrets.toml").write_text('token = "ntn_from_secrets"\n')
    assert _load_token({}, str(tmp_path / "config.toml")) == "ntn_from_secrets"


def test_secrets_file_wins_over_a_token_left_in_config(tmp_path):
    (tmp_path / "config_secrets.toml").write_text('token = "ntn_from_secrets"\n')
    assert _load_token({"token": "ntn_old"}, str(tmp_path / "config.toml")) == "ntn_from_secrets"


def test_token_in_config_still_works_with_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="sync"):
        token = _load_token({"token": "ntn_old"}, str(tmp_path / "config.toml"))
    assert token == "ntn_old"
    assert any("config_secrets.toml" in m for m in caplog.messages)


def test_explicit_secrets_path(tmp_path):
    other = tmp_path / "elsewhere.toml"
    other.write_text('token = "ntn_explicit"\n')
    assert _load_token({}, str(tmp_path / "config.toml"), str(other)) == "ntn_explicit"


def test_no_token_anywhere_returns_none(tmp_path):
    assert _load_token({}, str(tmp_path / "config.toml")) is None
