# Sandbox: aiogram-dialog оценка

Изолированные пробы новых библиотек/паттернов до интеграции в основной код.

Сейчас здесь:

- [new_group_dialog.py](new_group_dialog.py) — порт текущего `NewGroup` мастера (style → level → weekdays → time → venue → room → teachers) на `aiogram-dialog`. Цель — оценить:
  - сколько строк уходит/добавляется;
  - как ложатся «Назад»-кнопки на `SwitchTo`/`Back`;
  - как ложатся style-picker / weekdays multi-select / venue-picker на встроенные виджеты;
  - какие проблемы с тестами (aiogram-dialog тесты идут отдельно от наших `FakeBot`-тестов).

Этот код **не подключается** к [app/main.py](../app/main.py) — он только импортится для дымового запуска и оценки.

Запуск проверки импорта:
```powershell
python -c "import _sandbox.new_group_dialog as m; print(m.dialog)"
```

Решение по миграции принимается ПОСЛЕ оценки. Если решим брать — переносим в `app/bot/dialogs/` и подключаем в dispatcher. Если нет — `_sandbox/` удаляется и `aiogram-dialog` уходит из dependencies.
