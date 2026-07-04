"""Single-instance file-lock — второй процесс с тем же lock-файлом не должен
пройти, а после освобождения захват снова доступен."""
from app.core.instance_lock import InstanceLock


def test_second_acquire_fails_then_succeeds_after_release(tmp_path) -> None:
    lock_path = tmp_path / "dance.sqlite3.lock"

    first = InstanceLock(lock_path)
    assert first.acquire() is True

    second = InstanceLock(lock_path)
    assert second.acquire() is False  # другой экземпляр уже держит lock

    first.release()

    third = InstanceLock(lock_path)
    assert third.acquire() is True
    third.release()
