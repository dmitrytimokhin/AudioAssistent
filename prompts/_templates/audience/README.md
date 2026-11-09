Шаблон аудитории. Копируется командой `new audience <имя>` (или `lab.new("audience", "<имя>")` в ноутбуке).
- system.md: роль и правила стиля. Главное место для оптимизации.
- person.md: запрос на одного сотрудника. Переменные `${date} ${project_id} ${full_name} ${previous_status} ${examples} ${utterance}`.
- examples.md: 2–4 образца желаемого стиля (подставляются в `${examples}`).
- audience.yaml: раскладка итога: `{date} {project} {index} {full_name} {text}`.
