"""main() должен транслировать провал run() в ненулевой exit-код — иначе
Task Scheduler не видит падение и не перезапускает бота."""
import pytest

from app import main as main_module


async def _fake_run_ok() -> bool:
    return True


async def _fake_run_fail() -> bool:
    return False


async def _fake_run_keyboard_interrupt() -> bool:
    raise KeyboardInterrupt


def test_main_no_exit_on_success(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "run", _fake_run_ok)
    main_module.main()  # не должно поднять SystemExit


def test_main_exits_nonzero_on_run_failure(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "run", _fake_run_fail)
    with pytest.raises(SystemExit) as exc_info:
        main_module.main()
    assert exc_info.value.code != 0


def test_main_keyboard_interrupt_is_clean_exit(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "run", _fake_run_keyboard_interrupt)
    main_module.main()  # Ctrl+C — не поднимает SystemExit с ненулевым кодом
