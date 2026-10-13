import pytest

from app.graph import NodeSpec, PathError, normalize_path, validate_tree


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("/", "/"), ("/var//www/", "/var/www"), ("/var/./www", "/var/www"), ("/a/b/../c", "/a/c")],
)
def test_normalize(raw, expected):
    assert normalize_path(raw) == expected


@pytest.mark.parametrize("bad", ["", "var/www", "/..", "/a/../..", "/a\x00b", "\\windows", "/a\\b"])
def test_normalize_rejects_escape_and_garbage(bad):
    with pytest.raises(PathError):
        normalize_path(bad)


def _tree():
    return [
        NodeSpec("r", None, "/", "directory"),
        NodeSpec("v", "r", "/var", "directory"),
        NodeSpec("w", "v", "/var/www", "directory"),
        NodeSpec("f", "w", "/var/www/index.html", "file"),
    ]


def test_valid_tree():
    assert validate_tree(_tree()) == []


def test_missing_parent():
    t = _tree() + [NodeSpec("x", "nope", "/x", "file")]
    assert any("parent does not exist" in e for e in validate_tree(t))


def test_two_roots():
    t = _tree() + [NodeSpec("r2", None, "/", "directory")]
    errs = validate_tree(t)
    assert any("exactly one root" in e for e in errs)


def test_path_not_child_of_parent():
    t = _tree() + [NodeSpec("x", "w", "/etc/passwd", "file")]
    assert any("not a child" in e for e in validate_tree(t))


def test_file_cannot_be_parent():
    t = _tree() + [NodeSpec("x", "f", "/var/www/index.html/x", "file")]
    assert any("not a directory" in e for e in validate_tree(t))


def test_unnormalized_and_duplicate_paths():
    t = _tree() + [NodeSpec("d", "w", "/var/www/../www/a", "file")]
    assert any("not normalized" in e for e in validate_tree(t))
    t2 = _tree() + [NodeSpec("d", "w", "/var/www/index.html", "file")]
    assert any("duplicate path" in e for e in validate_tree(t2))
