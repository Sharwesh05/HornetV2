from app.runtime.driver import FsEntry
from app.runtime.fsdiff import diff, parse_find


def test_parse_handles_odd_names_and_garbage():
    raw = b"f\t5\t1.5\t/tmp/a b\0d\t0\t2.0\t/tmp/we\tird\0garbage\0f\tx\t1\t/bad\0"
    got = parse_find(raw)
    assert [e.path for e in got] == ["/tmp/a b", "/tmp/we\tird"]


def test_diff_created_modified_deleted():
    before = [FsEntry("/tmp/a", "f", 1, 1.0), FsEntry("/tmp/b", "f", 1, 1.0), FsEntry("/tmp/c", "f", 1, 1.0)]
    after = [FsEntry("/tmp/a", "f", 1, 1.0), FsEntry("/tmp/b", "f", 9, 2.0), FsEntry("/tmp/d", "f", 3, 3.0)]
    ops = {(c["path"], c["operation"]) for c in diff(before, after)}
    assert ops == {("/tmp/b", "modified"), ("/tmp/c", "deleted"), ("/tmp/d", "created")}
    assert diff(after, after) == []


def test_directory_mtime_noise_is_ignored_but_type_change_is_not():
    before = [FsEntry("/tmp", "d", 40, 1.0), FsEntry("/tmp/x", "f", 1, 1.0)]
    after = [FsEntry("/tmp", "d", 80, 9.0), FsEntry("/tmp/x", "d", 1, 1.0)]
    assert [(c["path"], c["operation"]) for c in diff(before, after)] == [("/tmp/x", "modified")]
