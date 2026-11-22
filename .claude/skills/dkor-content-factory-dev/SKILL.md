---
name: dkor-content-factory-dev
description: Инструкция по разработке и поддержке DKORWORLD2 Content Factory — структура проекта, установка с нуля, тесты, валидация, HTML-предпросмотр, безопасный dry-run, добавление новой площадки, диагностика, проверки перед коммитом и аудит секретов. Используй при доработке кода, тестов, README и скиллов проекта. Ничего не публикует и не отправляет.
---

# Разработка Content Factory

## Назначение
Контент-завод превращает один проверенный источник об ИИ в черновики для Telegram, X, YouTube Shorts и dkor.space (JSON `dkor-social/v1`), проверяет их кодом и независимым ревьюером и показывает результат в предпросмотре или dry-run. Скилл описывает, как безопасно менять сам проект. Для запуска контент-цикла на конкретном источнике есть `/dkor-content-pipeline`.

## Структура
- `scripts/validate_post.py` — валидатор и единственный источник числовых лимитов (`TELEGRAM_MIN_CHARS`, `TELEGRAM_MAX_CHARS`, `X_MAX_CHARS`).
- `scripts/preview.py` — генератор `temp/preview.html`; лимиты импортирует из валидатора.
- `scripts/tg_publisher.py` — Telegram: dry-run по умолчанию, `check-access`, `send` только с подтверждением.
- `scripts/cf_images.py`, `scripts/mcp_server.py`, `scripts/run_mcp.py` — генерация изображений (Cloudflare Workers AI) и локальный MCP-сервер; `run_mcp.py` — переносимый запуск.
- `scripts/redact.py` — маскирование секретов; `scripts/test_*.py` — тесты.
- `posts/`, `sources/`, `reports/`, `assets/`, `images/` (скриншоты README), `temp/` (в git не попадает).
- `.claude/skills/`, `.claude/agents/` — скиллы и субагенты.

## Установка с нуля (Windows, PowerShell)
```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env   # затем вручную подставьте свои значения; .env в git не попадает
```
Ядро (валидатор, предпросмотр) работает на стандартной библиотеке; `mcp` и `httpx` нужны для MCP, Cloudflare и Telegram.

## Тесты
```powershell
$py = ".\.venv\Scripts\python.exe"
foreach ($t in "validate_post","cf_images","mcp_server","tg_publisher","preview") { & $py "scripts\test_$t.py" }
```
Тесты не используют сеть и настоящие токены. `GitHygiene`-тесты требуют git-репозиторий (`git init`).

## Валидация и предпросмотр
```powershell
& $py scripts\validate_post.py posts\<file>.json
& $py scripts\preview.py          # пишет temp\preview.html, откройте в браузере
```

## Безопасный dry-run
`& $py scripts\tg_publisher.py dry-run posts\<file>.json` — без сети и без чтения `.env`. Реальная отправка требует `TG_ALLOW_LIVE_SEND=1`, fingerprint из dry-run и явного разрешения владельца в этом разговоре. Генерация изображений: `dry_run=True` по умолчанию, живая — только при `dry_run=False` и `CF_ALLOW_LIVE_GENERATION=1`. **Никогда не запускай `send`, `check-access` и живую генерацию без прямого разрешения владельца.**

## Скиллы и субагенты
- `/dkor-content-pipeline` — оркестратор полного цикла (источник → исследователь → черновик → валидатор → ревьюер → отчёт).
- `/dkor-ai-news-to-social` — правила написания черновика и формат JSON.
- `dkor-source-researcher` — собирает факты со ссылками, возвращает JSON, файлы не меняет.
- `dkor-content-reviewer` — независимый вердикт PASS/FAIL, файлы не меняет.
Не меняй их без необходимости; после правки проверь frontmatter настоящим YAML-парсером (PyYAML): должны быть `name` и `description`.

## Добавление новой площадки
1. Расширь формат в скилле `dkor-ai-news-to-social` и валидаторе `validate_post.py`; числовые лимиты — константами в валидаторе.
2. Добавь тесты валидатора для хорошего и плохого случая.
3. Добавь карточку в `preview.py` (лимиты импортировать, текст экранировать `html.escape`) и тест в `test_preview.py`.
4. Обнови README и правила в `CLAUDE.md`. Существующие посты не ломай: новое поле — необязательное либо с миграцией.

## Диагностика
- Валидатор: читай номер и текст ошибки; частые причины — длина Telegram/X, BOM, `image_path` вне `assets/`/`posts/`, непустой `published` у `draft`.
- Предпросмотр: сломанный JSON показывается отдельной карточкой; нет картинки — заглушка.
- MCP: `python scripts\run_mcp.py` должен молча ждать stdio; ошибки идут в stderr. Нет `mcp` — проверь venv.
- Кодировка: все файлы UTF-8 без BOM; не пиши JSON через `>` в PowerShell 5.1.
- Тест `GitHygiene` падает с кодом 128 — нет git-репозитория.

## Перед коммитом
1. Все `scripts/test_*.py` проходят (посчитай количество, не округляй).
2. `validate_post.py` — OK для каждого `posts/*.json`.
3. `git status` и `git add -A --dry-run`: в списке нет `.env`, `.venv/`, `temp/`, `.claude/settings.local.json`.
4. Аудит секретов (ниже). Коммит и push — только по команде владельца; `git config --local user.email` должен оканчиваться на `@users.noreply.github.com`.

## Аудит секретов перед публикацией
Ищи в отслеживаемых файлах: токены и ключи API, реальные ID каналов (допустим только плейсхолдер `-1001234567890`), личные e-mail, абсолютные пути (`C:\Users\`, `D:\`), приватные ссылки, имена локальных пользователей и посторонних проектов. Выводи в отчёт только путь, категорию и количество — не сами значения. Совпадения в тестовых заглушках проверяй вручную: они должны быть явно фиктивными.

## Запреты
Не отправляй сообщения, не вызывай платные API, не коммить и не пушь, не создавай и не открывай репозитории, не печатай секреты — без явного подтверждения владельца.
