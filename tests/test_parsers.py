import json

from app.parsers import try_parse_json_or_table


def test_parse_json():
    data = {"items": [{"name": "alice"}, {"name": "bob"}]}
    out = try_parse_json_or_table(json.dumps(data))
    assert out == {"data": data}


def test_parse_table_whitespace():
    table = """
NAME    ID    STATUS
alice   1     active
bob     2     inactive
""".strip()
    out = try_parse_json_or_table(table)
    assert out == {
        "data": [
            {"NAME": "alice", "ID": "1", "STATUS": "active"},
            {"NAME": "bob", "ID": "2", "STATUS": "inactive"},
        ]
    }


def test_parse_table_pipe():
    table = """
NAME | ID | STATUS
alice | 1 | active
""".strip()
    out = try_parse_json_or_table(table)
    assert out == {
        "data": [
            {"NAME": "alice", "ID": "1", "STATUS": "active"},
        ]
    }

