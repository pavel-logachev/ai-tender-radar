# Lead triage evaluation dataset

Здесь хранится локальный versioned golden/regression dataset для `llm_customer_lead_triage`.

Обязательные границы:

- реальные кейсы добавляются только в санитизированном виде с устойчивым `case_id`, provenance и SHA-256 источника;
- тексты закупок, ответы модели, персональные данные и клиентские документы не копируются в глобальную память;
- synthetic fixtures проверяют механику evaluator, но не дают release approval;
- решение о релизе требует `real_case_count > 0`, нулевых critical errors, 100% structured validity и прохождения абсолютных порогов;
- release gate остаётся закрытым без 100% покрытия latency/cost, заданных эксплуатационных порогов, завершённого обязательного human review и grounding review каждого кейса;
- prompt, model, schema, code revision и dataset hash должны быть привязаны к отдельному evaluation run.

Проверка структуры dataset:

```powershell
.venv\Scripts\python.exe -m scripts.evaluate_lead_triage --dataset <path-to-dataset.json>
```

Оценка сохранённых predictions:

```powershell
.venv\Scripts\python.exe -m scripts.evaluate_lead_triage --dataset <dataset.json> --predictions <predictions.json> --max-p95-latency-ms <ms> --max-average-cost-units <units> --max-fallback-rate <0..1> --max-human-review-rate <0..1>
```

Первый реальный dataset намеренно не создаётся автоматически: его нужно собрать из подтверждённого feedback, проверить приватность и зафиксировать веса ошибок до просмотра candidate outputs.
