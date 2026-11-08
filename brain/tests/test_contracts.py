from src.common import schemas


def test_schema_files_are_up_to_date():
    for name, text in schemas.rendered().items():
        path = schemas.SCHEMA_DIR / name
        assert path.is_file(), f"run python -m src.common.schemas ({name} missing)"
        assert path.read_text() == text, f"run python -m src.common.schemas ({name} out of date)"
