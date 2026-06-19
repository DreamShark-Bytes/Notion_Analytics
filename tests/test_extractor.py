from extractor import sanitize_col


def test_basic_lowercase():
    assert sanitize_col("Name") == "name"


def test_space_to_underscore():
    assert sanitize_col("Due Date") == "due_date"


def test_multiple_spaces_collapse():
    assert sanitize_col("A  B  C") == "a_b_c"


def test_special_chars_replaced():
    assert sanitize_col("Field #1") == "field_1"
    assert sanitize_col("Field (Notes)") == "field_notes"


def test_leading_digit_prefixed():
    assert sanitize_col("1st Task") == "_1st_task"


def test_consecutive_separators_collapse():
    assert sanitize_col("A--B") == "a_b"
    assert sanitize_col("A  --  B") == "a_b"


def test_all_special_falls_back():
    assert sanitize_col("!!!") == "col"
    assert sanitize_col("___") == "col"


def test_already_clean():
    assert sanitize_col("status") == "status"


def test_notion_property_names():
    assert sanitize_col("Blocked by") == "blocked_by"
    assert sanitize_col("Closed Date") == "closed_date"
    assert sanitize_col("Is Open") == "is_open"
