"""Тесты на сам тестовый double FakeBot (tests/_fakes.py) — используется почти
во всех e2e-тестах, поэтому расхождение с реальным Bot API стоит ловить явно."""
from tests._fakes import FakeBot


async def test_edit_message_text_mutates_existing_message() -> None:
    """Настоящий Telegram edit_message_text правит СУЩЕСТВУЮЩЕЕ сообщение —
    держатель исходной ссылки (напр. B8 picker_ref) должен увидеть новый текст,
    а не остаться со старым, пока где-то создаётся отдельный объект."""
    bot = FakeBot()
    original = await bot.send_message(100, "старый текст")
    await bot.edit_message_text(
        "новый текст", chat_id=100, message_id=original.message_id
    )
    assert original.text == "новый текст"
    assert len(bot.sent) == 1  # не задвоилось отдельным сообщением


async def test_edit_message_text_creates_new_when_unknown() -> None:
    """Если message_id незнаком (не через этот FakeBot) — не падаем, заводим
    новую запись, как и раньше."""
    bot = FakeBot()
    result = await bot.edit_message_text("текст", chat_id=200, message_id=9999)
    assert result.text == "текст"
