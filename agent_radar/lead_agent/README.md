# Агент исследования лидов

Агент получает одну закупку, читает документы и исследует заказчика. Поведение задаёт [публичный пример промпта](prompt.md), который оператор дополняет своим профилем. Рабочий промпт не опубликован.

```text
analysis-source.json -> runner -> agent -> grounding -> leads.sqlite3
    -> delivery -> Telegram -> feedback / comments -> export
                состояние этапов -> health
```

## Входной пакет

`runner --source` принимает JSON с полем `rows`. Каждая строка содержит `card` с идентификатором, названием и документами; `opportunity` передаёт заказчика, статус и даты. Опциональное `gaps` отмечает пропуски, например ожидающие загрузки документы. Ниже синтетический пример без контактов:

```json
{
  "rows": [{
    "card": {
      "id": "example-source:1",
      "title": "Поставка серверов",
      "customer_name": "ООО Учебный заказчик",
      "source_url": "https://tenders.example.org/1",
      "description": "Нужны два сервера",
      "documents": [{"id": "spec-1", "title": "ТЗ", "text": "Требуется два сервера для вычислений"}]
    },
    "opportunity": {
      "buyer": {"name": "ООО Учебный заказчик", "inn": "0000000000"},
      "status": "proposal",
      "publication_date": "2026-10-01T00:00:00Z",
      "acceptance_end_date": "2026-11-01T00:00:00Z"
    },
    "gaps": []
  }]
}
```

`store.version` считает отпечаток только доступных агенту данных. Уникальная пара закупка/версия защищает от повторного исследования. Новая версия уже отправленной закупки получает отметку об обновлении. Если документы ещё стоят в очереди загрузки и до конца приёма больше 24 часов, исследование ждёт.

## Запуск

После установки зависимостей из корня репозитория:

```text
python -m agent_radar.lead_agent.runner --source <analysis-source.json> --state-root <private-state-directory> --limit 8
python -m agent_radar.lead_agent.delivery --state-root <private-state-directory>
python -m agent_radar.lead_agent.health --bundle-root <bundle-directory> --state-root <private-state-directory>
```

Исследователю нужен `OPENROUTER_API_KEY`. Параметры `--model`, `--fallback-model` и `--no-fallback` управляют моделями; по умолчанию используются Qwen 3.7 Flash и DeepSeek v4.1 Flash. Веб-поиску нужен необязательный `ddgs`; без него агент увидит сообщение о недоступности поиска. Доставка и health используют переменные доступа Telegram, которые задаёт оператор, и не создают поллер. Они отправляют сообщения при явном запуске; рабочее расписание доставки по будням задаётся внешним планировщиком и в публичный репозиторий не входит.

## Доставка и менеджер

До сохранения карточки `grounding` проверяет наличие контактов в прочитанном тексте и пересчитывает A/B/C. На доставку идут только `lead` с A/B. Просроченная закупка не отправляется. Неоднозначный исход отправки не повторяется; после 429 резервирование освобождается.

`telegram_menu.wire_telegram_menu` принимает готовое `Application` и адаптер с методами `is_authorized`, `should_process`, асинхронными `build_control_event` и `handle_message`. Для команды `/data` можно передать `freshness_reader`; он возвращает `source_collected_at` и `source_window_from`. Регистрация выполняется до общих обработчиков приложения. Вызывающая сторона авторизует обновления до dispatch. Разрешённые частные пользователи дополнительно проверяются по `TELEGRAM_ALLOWED_USERS`.

`RADAR_LEAD_FEEDBACK=1` включает кнопки и заметки, `RADAR_DIGEST_STATE_ROOT` задаёт каталог журнала и подписок. Заметки принимаются только reply на доставленную карточку своего чата; текст заметки не идёт модели. `export.build_workbook(kind="new"|"work")` создаёт две пассивные таблицы из текущего состояния.

## Проверки

```powershell
$env:PYTHONUTF8='1'
python -m unittest tests.test_agent_radar_lead_agent tests.test_agent_radar_telegram_menu
```

Тесты покрывают инструменты, редиректы, бюджеты агента, grounding, версии, дедупликацию, истёкшие сроки, повторные карточки, отправку, кнопки, заметки и Excel. Интеграционные проверки меню используют настоящий `python-telegram-bot.Application` с синтетическим ботом без сетевых запросов.
