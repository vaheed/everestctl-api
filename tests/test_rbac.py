import os
import rbac


def test_append_policy_lines(tmp_path):
    policy = tmp_path / "policy.csv"
    path, count = rbac.append_policy_lines(str(policy), ["p, role:x, resource, read, ns/*", "g, alice, role:x"])
    assert os.path.exists(path)
    assert count == 2
    with open(path) as f:
        content = f.read()
        assert "alice" in content

