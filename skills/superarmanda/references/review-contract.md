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

`model` — наблюдаемые CLI metadata, если они доступны. Самоописание из текста LLM не является
доказательством модели, аккаунта или provider identity. Для GitHub Codex используйте ID review/
request как `session_id`; не приписывайте сервису конкретную модель без подтверждения GitHub.

При записи результата `reviewed_head` — полный hexadecimal Git commit ID пакета, без сокращения
или нормализации. Cross-provider `pass` несёт `packet_hash` ровно в форме
`sha256:<64 lowercase hex>`; GitHub Codex `pass` вместо него несёт HTTPS URL evidence artifact.
State хранит эти значения как metadata. Coordinator подтверждает существование артефакта и
`gate_ready: true` adapter report перед записью pass.

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
