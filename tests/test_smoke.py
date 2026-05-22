"""Smoke tests that don't need a database."""
from app.services.groups import _gen_join_code


def test_join_code_unique() -> None:
    codes = {_gen_join_code() for _ in range(200)}
    assert len(codes) == 200


def test_join_code_url_safe() -> None:
    code = _gen_join_code()
    assert all(c.isalnum() or c in "-_" for c in code)
