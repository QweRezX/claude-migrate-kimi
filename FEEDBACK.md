# Feedback / Обратная связь

Found a bug, a rendering glitch, or a session that didn't come over?
Нашёл баг, кривой рендер или потерянную сессию?

Send two things to the person who gave you this archive
(or attach to a GitHub issue once the repo is public):
Пришли две вещи тому, кто дал тебе архив:

## 1. The report file / Файл отчёта

Run the import with `--report` (or re-run it — already imported sessions
are skipped, so it's safe):
Запусти импорт с флагом `--report` (повторный запуск безопасен — уже
импортированные сессии пропускаются):

```bash
python3 scripts/claude2kimi.py --all --report report.json
```

`report.json` contains tool/protocol versions, per-session message counts,
skipped event types and errors. **It never contains your message text** —
safe to share as-is.
Файл содержит версии, счётчики сообщений, пропущенные типы событий и ошибки.
**Текста переписки там нет** — можно отправлять как есть.

## 2. A few lines / Пара строк текстом

Copy this template, fill in what applies / скопируй и заполни:

```
OS / ОС:                    (macOS 15 / Windows 11 / ...)
Kimi Code version:          (Settings → About, or `kimi --version`)
Claude folder location:     (стандартное ~/.claude или свой путь?)
Sessions found / found ok:  (нашлось N из M?)
Sessions that open fine:    (сколько открываются и читаются)
Problems:                   (что именно не так: пустое окно, ошибка,
                             кракозябры, потерянные сообщения, другое)
Worst session (if any):     (название/файл сессии с проблемой)
```

## What helps most / Что помогает больше всего

- A screenshot of a broken session beats a paragraph of description.
  Скриншот сломанной сессии лучше абзаца описания.
- If the app showed an error toast — **Copy diagnostics** and paste it.
  Если было окно с ошибкой — кнопка **Copy diagnostics**, текст сюда.
- "It worked" is also feedback — say so! / «Всё работает» — тоже отзыв,
  не стесняйся написать.
