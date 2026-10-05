# Review contract

Внешний reviewer возвращает один JSON-объект, соответствующий
[`../schemas/review-result.schema.json`](../schemas/review-result.schema.json). Runner сверяет
`reviewed_head` и `packet_hash` с отправленным пакетом до записи результата.

- `pass` допустим только при полном пакете и явном завершённом ответе.
- `findings` содержит воспроизводимые findings; reviewer не правит код и не запускает агентов.
- `incomplete` перечисляет отсутствующий контекст и не закрывает gate.
- `error` фиксирует transport/auth/quota/timeout/JSON failure. Runner записывает timeout
  одного запуска без автоматического повторения; coordinator может явно повторить его один раз,
  сохранив оба artifacts. Временный transport failure допускает один повтор в рамках запуска.

Runner передаёт правила статусов непосредственно в инструкции reviewer:
`pass` требует пустых `findings` и `missing_context`; недостающий контекст
следует возвращать как `incomplete`, а `findings` требует непустого списка находок.

Для обоих провайдеров иначе корректный ответ `pass` с пустым `findings` и
непустым списком `missing_context` сохраняется как `incomplete`. Список
недостающего контекста остаётся в `response.missing_context`, поля `status` и
`response.status` становятся `incomplete`, `gate_ready` остаётся `false`, код выхода
ненулевой. Такой ответ не закрывает review gate и не теряется за общей ошибкой
`validation`.

Это только понижение статуса. Некорректная схема или типы, другой HEAD/packet,
`pass` с находками (даже при наличии `missing_context`), `incomplete` без
недостающего контекста и `findings` без находок по-прежнему отклоняются.
Проверки модели, подписки и изоляции инструментов сохраняются. Старые packet
envelope v1 не переписываются; автоматическое добавление полных файлов в пакет
этим изменением не вводится.

`model` — наблюдаемые CLI metadata, если они доступны. Самоописание из текста LLM не является
доказательством модели, аккаунта или provider identity. Для GitHub Codex используйте ID review/
request как `session_id`; не приписывайте сервису конкретную модель без подтверждения GitHub.

При записи результата `reviewed_head` — полный hexadecimal Git commit ID пакета, без сокращения
или нормализации (для репозитория с `--object-format=sha256` это полный 64-hex ID; сокращённый не
принимается). Cross-provider `pass` несёт `packet_hash`: `state.py task-result --packet-hash`
принимает и голый 64 lowercase hex (так его пишет `review.py` в envelope и в `packet_hash` ответа
ревьюера), и `sha256:<64 lowercase hex>`, а хранит всегда каноническую форму `sha256:<hex>`; верхний
регистр, другая длина и другой префикс отвергаются. Отчёт `review.py run` дополнительно несёт поле
`state_packet_hash` — готовое `sha256:<hex>` для state; `packet_hash` envelope не меняется, старые
пакеты загружаются как раньше. GitHub Codex `pass` вместо него несёт HTTPS URL evidence artifact.
State хранит эти значения как metadata. Coordinator подтверждает существование артефакта и
`gate_ready: true` adapter report перед записью pass.

## Ревью задачи с риском high (1.2.1, #86)

Задача с эффективным риском high (см. [profiles.md](profiles.md), «Модель роли по риску») проходит
только с двумя ревью ОДНОГО пакета на текущем HEAD: Astra и Fable. Документированная команда:

```bash
python3 "$SUPERARMANDA_DIR/scripts/review.py" run --repo /repo \
  --packet /outside/packet.json --profile claude-host --output /outside/review-astra.json
python3 "$SUPERARMANDA_DIR/scripts/review.py" run --repo /repo \
  --packet /outside/packet.json --profile codex-host --output /outside/review-fable.json
```

Затем оба отчёта записываются в manifest; `--packet-hash` — поле `state_packet_hash` отчёта:

```bash
python3 "$SUPERARMANDA_DIR/scripts/state.py" task-result --manifest <path> --task <id> \
  --role cross_provider_reviewer --status pass --session-id <id> --head <sha> \
  --artifact /outside/review-astra.json --reviewed-head <sha> --packet-hash <sha256:...>
python3 "$SUPERARMANDA_DIR/scripts/state.py" task-result --manifest <path> --task <id> \
  --role second_reviewer --status pass --session-id <id> --head <sha> \
  --artifact /outside/review-fable.json --reviewed-head <sha> --packet-hash <sha256:...>
```

Для такой задачи `state.py` не верит слову координатора, а сам читает отчёт из `--artifact`
(обычный файл, не симлинк, не больше 1 MiB, один JSON-объект) и отказывает, если: `profile` не из
`claude-host`, `codex-host`, `codex-host-opus`; `status` отчёта не равен записываемому; для `pass`
нет `gate_ready: true`; для `findings` нет `capabilities.primary_model_verified: true` или
`capabilities.tool_isolation` равен `unverified`; `state_packet_hash` не равен `--packet-hash`;
`response.reviewed_head` не равен HEAD. Отчёт обязан быть согласован сам с собой: `requested_model`
и `observed_models` должны принадлежать его профилю по таблице `PROFILE_MODELS` из `review.py`
(`claude-host` → `gpt-6-astra`; `codex-host` → запрошена `fable`, наблюдается `claude-fable-5-1`;
`codex-host-opus` → `claude-opus-5-5`), а `session_id` ревью — непустая строка. У Astra
`observed_models` — ровно один элемент; у Claude-профилей это объект `modelUsage`, где рядом с
основной моделью допустимы записи вспомогательных вызовов CLI, поэтому целиком он не
сравнивается: основная модель обязана в нём быть, а `capabilities.primary_model_verified: true`
и проверенная изоляция инструментов требуются и для `pass`, и для `findings`. Один и тот же
отчёт или одна сессия ревью не закрывает обе роли: вторая запись — отказ. В результат пишутся
`profile`, `artifact_sha256` (SHA-256 байтов отчёта) и `review_session_id`. `error`, `unavailable`
и `incomplete` отчёта не требуют и pass не дают. Известная граница: отчёт не подписан, поэтому
`state.py` проверяет его форму, согласованность и привязку к HEAD и пакету, но не доказывает его
подлинность.

Задача high готова, когда coder и tester прошли на Fable (`--model`, поле `model` результата —
`claude-fable-5-1`; при medium и low — `claude-sonnet-5-5` либо без модели), а оба ревью — `pass`
либо `findings`, покрытые `fix-loop --defer`/`--accept` ровно на этот результат, причём пара
профилей — ровно один `claude-host` и ровно один `codex-host` с одним `packet_hash`. Один Astra,
два Astra, Fable без Astra, ревью разных пакетов — не pass; причину называет `next_action`
команды `where`. Какая роль (`cross_provider_reviewer` или `second_reviewer`) несёт какой профиль,
не важно: важна пара. Находки второго ревьюера идут тем же `fix-loop`, что и у первого.

**Запасной Opus и quota evidence.** Fable-ревью с ошибкой квоты не закрывает gate и не
заменяется само. Единственный запасной маршрут — отдельный запуск `--profile codex-host-opus` на
том же пакете и запись его отчёта с `--quota-evidence <путь>`. Quota evidence — это отчёт ошибки
`review.py run --profile codex-host` с полями `status: error`, `error_category: quota`,
`gate_ready: false`, чьи `reviewed_head` и `state_packet_hash` равны HEAD и пакету записываемого
результата. Другого источника нет: текст диагностики, уведомление провайдера, отчёт другого HEAD
или пакета, отчёт без этих двух полей (написанный до 1.2.1) не подходят. В результат пишутся
`fallback_for: codex-host` и `quota_evidence: {artifact, sha256}`; оба пути видны в `where`.
`--quota-evidence` с любым другим профилем — отказ.

**Сказанное ревью о HEAD нельзя отменить.** Для задачи high `state.py` ведёт в записи задачи
дописываемую историю `review_history`: каждый записанный результат обеих ролей ревью (роль,
профиль, статус, HEAD, `tree_fingerprint`, `packet_hash`, `result_id`, дайджест результата
`result_sha256`, `artifact`, время). Историю не очищает ни новая запись роли, ни `resume`. Правила
ниже смотрят в историю текущего HEAD, а не только в текущие результаты:

- Запасной Opus заменяет только Fable, которая ревью НЕ дала. Если в истории на этом HEAD есть
  результат профиля `codex-host` со статусом `pass` или `findings` — в любой роли, покрытый
  `--defer`/`--accept` или нет, перезаписанный позже (`unavailable`, `error`, `incomplete`) или
  нет, — запись `codex-host-opus` — отказ. Прежний отчёт квоты его не отменяет.
- Находки не стираются. Пока в истории на этом HEAD есть `findings` роли ревью, не покрытые
  `fix-loop --defer`/`--accept` ровно на тот результат, задача не становится готовой, а новый
  `task-result` этой роли (любого статуса и профиля) — отказ. Это верно и после `resume`
  «туда-обратно» (тронули дерево, вернули): сверка идёт по HEAD, а не по дереву. `where` называет
  причину в `next_action` и показывает такие находки в `open_findings`.
- Выход прежний: `fix-loop --defer` или `--accept --source <роль>` — они привязываются к тому
  самому результату, даже если он уже ушёл из текущих (привязка по дайджесту из истории), — либо
  исправление кода: новый коммит это другой HEAD, история прежнего к нему не относится, и
  результаты пишутся свободно. Ложную находку координатор принимает
  `fix-loop --accept --severity low --note "<почему>"` и после этого может повторить ревью на том
  же HEAD. `incomplete`, `error` и `unavailable` до первого `pass`/`findings` повторяются свободно.
- Принятое ограничение остаётся ограничением этого HEAD: `where.accepted_limitations` показывает
  запись `--accept` и после того, как её результат заменён другим.

При риске medium и low и для manifest `version: 1` история не ведётся, ключа `review_history` нет,
повторная запись, как и раньше, заменяет результат. Если риск задачи подняли до high, её текущие
результаты ревью попадают в историю в момент подъёма.

Категорию `quota` у Claude-профилей `review.py` выводит только из сигнала провайдера: событие
потока `assistant`, которое CLI пишет сам (`message.model: "<synthetic>"`, `is_api_error_message:
true`), с верхнеуровневым `error: "rate_limit"`; если событие `result` несёт `api_error_status`, он
обязан быть 429. Маршрут проверен на живой форме события ошибки провайдера в потоке (снимок Claude
Code 2.1.289, HTTP 404 — `tests/fixtures/transcripts/stream-provider-error.jsonl`) и на живом
значении `rate_limit` со статусом 429 из журнала сессии (`rate-limit-assistant.jsonl`). Живого
потока при исчерпанной подписке нет (#91): если он окажется другим, `quota` не будет и запасной
Opus не откроется — ошибка возможна только в закрытую сторону. Stdout потока текстовой эвристикой
не читается вовсе: прочие категории сбоя Claude-профиля берутся из stderr. Слова «quota» и «rate limit» в тексте
модели, в цитатах пакета, в stderr и в выводе `claude auth status` категорию не дают: текстовая
эвристика `quota` больше не возвращает. Неоднозначность закрывается в сторону «не evidence»:
код авторизации в потоке (`authentication_failed` и родственные) или признак auth, таймаута,
транспорта, отказа в stderr рядом с `rate_limit` — это соответствующая категория, а не `quota`;
событие с повторным ключом — не сигнал. У Astra категория идёт из `codexErrorInfo:
"usageLimitExceeded"` текущего thread/turn, то есть тоже из структуры, а не из текста. Auth, таймаут, отказ модели и любая другая
недоступность Fable — `error`/`unavailable` роли `second_reviewer`: задача не pass, отката на
Sonnet, Astra вторым разом или иную модель нет.

**Поля отчёта ошибки.** Отчёт `status: error` несёт `profile`, `attempts`, `error`,
`error_category`, `gate_ready: false`, а с 1.2.1 ещё `reviewed_head` (полный HEAD пакета) и
`state_packet_hash` (`sha256:<hex>`) — только когда конверт пакета загружен и сверен с
репозиторием. При ошибке раньше (нечитаемый или изменённый пакет, HEAD ушёл) этих полей нет.
Сырых диагностик и содержимого пакета в отчёте по-прежнему нет.

При риске medium и low, а также для manifest `version: 1`, действует прежнее правило: одно
ревью `cross_provider_reviewer`, отчёт state не разыменовывает.

Runner принимает только packet envelope v1. Его лимит измеряет весь сериализованный
envelope вместе с завершающим newline, хотя `packet_hash` остаётся SHA-256
канонического внутреннего payload. Ошибка packet или CLI event даёт nonzero
structured `status:error` с `gate_ready:false`, если output доступен и разрешён.
Для запрещённого или недоступного output возвращаются nonzero и безопасная
ошибка stderr без обещания artifact. Diagnostics ограничены allowlisted
category и не содержат raw packet или системную ошибку.
JSON object keys must be unique at every depth after escape decoding; duplicate
keys and excessive nesting fail before authentication or review execution.

Для нового packet builder v1 содержит committed regular blobs (mode `100644` или
`100755`) из declared HEAD, прочитанные по OID, и canonical Git rendering с
фиксированными prefixes, full index, histogram, context и запретом ext-diff,
textconv, colour и rename detection. Runner перед auth повторяет проверку base
ancestry, rendering и context against current repository. Старый envelope v1 не
переписывается; если он не проходит эту более строгую проверку, coordinator
строит новый packet, а старый evidence остаётся историческим artifact.

Пути requirements и test evidence в packet — только basenames; абсолютные локальные пути
не становятся частью evidence. До записи packet builder отклоняет output, совпадающий с
requirements, evidence или context input через тот же путь, symlink или hardlink. До запуска
auth или CLI runner так же отклоняет output, совпадающий с packet input.
All packet cleanliness checks include submodules even when `.gitmodules` asks
Git to ignore them. Before such a check, initialized submodules are bounded to
32 levels and their Git routing and filter configuration are audited.
Status runs separately in every audited root: the ignore override is not
inherited by Git's recursive child status processes.

Astra uses `scripts/codex_review.py` and the installed Codex App Server stdio protocol.
The process receives disabling overrides before startup; effective config, exact
subscription auth and server model/provider are checked before sending the packet.
Both thread and turn have no environments. Passive metadata notifications are not
model tool calls; tools, approvals, reroutes and mismatched event IDs fail closed.
Only one structured answer preceding a matching terminal completion is accepted.
Opaque thread/turn IDs and allowlisted usage/capabilities are retained; account
payloads, configuration and raw server diagnostics are not copied into reports.
The Astra adapter has a single deadline across setup and inference. A failed call
is preserved as an error; the coordinator may explicitly retry a transient failure
once, keeping both artifacts. Never retry auth/quota failures automatically.

Исчерпанная подписка Astra: после `turn/started` App Server шлёт `account/rateLimits/updated`,
`thread/status/changed {status:{type:"systemError"}}`, notification `error` с
`codexErrorInfo: "usageLimitExceeded"` и `turn/completed` со статусом `failed`. `systemError`
сам по себе ревью не обрывает: адаптер читает дальше в рамках общего дедлайна до терминального
`error` или `turn/completed` текущего thread/turn; без терминального события остаётся прежний
`timeout`. `usageLimitExceeded` (из `error` или из `turn.error`) даёт `error_category: "quota"`,
прочие `codexErrorInfo` — `completion`. Notification `error` принимается только для текущих
`threadId` и `turnId` (иначе `protocol`) и никогда не бывает pass: `gate_ready` остаётся `false`.
`error`, пришедший раньше ответа на `turn/start`, откладывается и проверяется после получения turn id
(только для `turn/start`; при `initialize`, `config/read`, `thread/start` — прежний `protocol`).
Все отложенные `error` проверяются до чтения любого completion (успешный `turn/completed` в очереди их не перекрывает). `willRetry: true` не делает `error` нетерминальным. Текст причины и account/plan payload в отчёт не копируются, только категория.

Бинарные изменения определяет `git diff --numstat -z` без опций патча: изменение бинарное, только
если ПЕРВЫЕ ДВА поля записи равны `-`. Содержимое файлов (например ячейка `-` в TSV) и имена с
табами бинарником не считаются. Изолированное bare-представление объектов создаётся с тем же
`--object-format` (sha1 или sha256), что и у исходного репозитория.

Astra accepts the default 512 KiB packet bound; `--max-bytes` can lower the loader
limit but does not raise Astra's fixed prompt cap. A larger custom packet can be
reviewed by Fable, while Astra returns a structured `input` failure. Size overrides
never bypass the adapter's bounded input contract.

For `codex-host`, Fable marks the primary model verified only when init metadata identifies
`claude-fable-5-1`, at least one assistant event is present and every assistant
event identifies that model, its `modelUsage` has a
`claude-fable-5-1` entry, and no `model_refusal_fallback` event is present.
Additional `modelUsage` entries may describe ancillary CLI calls and remain in the
report; for example, observed `claude-haiku-4-5-20251001` does not invalidate
verified primary Fable evidence.

`codex-host-opus` is a separate fixed Claude CLI profile. It passes primary
model verification only when init metadata, every assistant event, and a
`modelUsage` entry all identify exactly `claude-opus-5-5`, with at least one
assistant event and no `model_refusal_fallback`. It retains the same empty
tools/MCP/plugins and StructuredOutput-only checks. The runner neither routes
to this profile nor accepts a caller-supplied model.
