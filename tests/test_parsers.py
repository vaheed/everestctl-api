from app.parsers import parse_accounts_output


def test_parse_json():
    text = '{"items":[{"name":"alice"}]}'
    out = parse_accounts_output(text)
    assert "data" in out
    assert out["data"]["items"][0]["name"] == "alice"


def test_parse_table_pipe():
    text = """
    NAME | EMAIL | ROLE
    alice | a@example.com | user
    bob | b@example.com | admin
    """
    out = parse_accounts_output(text)
    assert isinstance(out["data"], list)
    assert out["data"][0]["name"] == "alice"


def test_parse_table_whitespace():
    text = """
    NAME    EMAIL                 ROLE
    alice   a@example.com         user
    bob     b@example.com         admin
    """
    out = parse_accounts_output(text)
    assert out["data"][1]["role"] == "admin"

