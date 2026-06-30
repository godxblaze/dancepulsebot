# E2E — настоящее прокликивание бота

> **Статус (2026-06-27):** активен **web-путь** — [web/RUNBOOK.md](web/RUNBOOK.md) (там же
> текущее состояние и «как продолжить»). Telethon-путь (`live/`) заброшен: создание
> api-приложения на my.telegram.org блокируется регионом (ERROR). Оставлен на случай VPN.

Эти тесты гоняют **живого** `@dancepulsebot` через настоящий Telegram (в отличие
от `tests/`, которые дёргают хендлеры через `FakeBot` без сети). Два слоя, у каждого
своя роль:

| Слой | Чем | Что даёт | Когда |
|------|-----|----------|-------|
| [`live/`](live/) | **Telethon** (user-account, MTProto) | headless, скриптуемый, повторяемый suite; настоящие сообщения и клики inline-кнопок | основной автотест, регрессии, CI |
| [`web/`](web/) | **Playwright MCP** (web.telegram.org) | я кликаю кнопки в видимом браузере + скриншоты | визуальные спот-чеки, демо |

Оба требуют **отдельного Telegram-аккаунта-«тестера»** (не бот; лучше не основной —
сессии могут конфликтовать). С точки зрения бота это неотличимо от живого человека.

## live/ — Telethon (рекомендуемый suite)

Одноразовая настройка:

```powershell
pip install -e ".[e2e]"

# 1. api_id / api_hash с https://my.telegram.org → API development tools — в .env:
#    E2E_API_ID=...   E2E_API_HASH=...
# 2. одноразовый логин (телефон + код, и 2FA-пароль если есть):
python -m e2e.live.login
#    → скопируй напечатанную строку E2E_SESSION=... в .env
```

Запуск:

```powershell
pytest e2e/live -v
```

Дальше — headless, креды в `.env` (всё в .gitignore). Suite не подхватывается
обычным `pytest` (он смотрит только `tests/`) и Stop-hook'ом — гоняется только явно.

Шаблоны клика — в [live/test_smoke.py](live/test_smoke.py); хелперы (`ask`,
`tap_inline`) — в [live/helpers.py](live/helpers.py). Reply-кнопки нижнего меню
«нажимаются» отправкой их текста, inline-кнопки — через `tap_inline`.

## web/ — Playwright (визуальные спот-чеки)

Я открываю web.telegram.org в настоящем браузере и кликаю кнопки. Настройка и
сценарии — в [web/RUNBOOK.md](web/RUNBOOK.md). Нужен Node.js (для `npx`); MCP-сервер
прописан в [`.mcp.json`](../.mcp.json).
