from src.core.diff_parser import extract_added_lines, parse_diff_changes, should_skip_file


def test_should_skip_lock_files():
    assert should_skip_file("package-lock.json")
    assert should_skip_file("yarn.lock")
    assert should_skip_file("pnpm-lock.yaml")
    assert should_skip_file("go.sum")


def test_should_skip_binary_files():
    assert should_skip_file("logo.png")
    assert should_skip_file("font.woff2")


def test_should_not_skip_source_files():
    assert not should_skip_file("main.py")
    assert not should_skip_file("src/App.vue")
    assert not should_skip_file("service/UserService.java")


def test_parse_diff_changes_filters_deleted():
    changes = [
        {"new_path": "deleted.py", "deleted_file": True, "diff": "- old code"},
        {"new_path": "main.py", "deleted_file": False, "diff": "+ new code"},
    ]
    result = parse_diff_changes(changes)
    assert len(result) == 1
    assert result[0]["file_path"] == "main.py"


def test_parse_diff_changes_filters_lock_files():
    changes = [
        {"new_path": "package-lock.json", "deleted_file": False, "diff": "+ something"},
        {"new_path": "src/index.js", "deleted_file": False, "diff": "+ code"},
    ]
    result = parse_diff_changes(changes)
    assert len(result) == 1
    assert result[0]["file_path"] == "src/index.js"


def test_extract_added_lines():
    diff = """@@ -1,3 +1,5 @@
 existing line
+new line 1
+new line 2
 another existing
"""
    lines = extract_added_lines(diff)
    assert len(lines) == 2
    assert lines[0] == (2, "new line 1")
    assert lines[1] == (3, "new line 2")
