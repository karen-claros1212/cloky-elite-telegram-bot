# Auditoría de v2.2.1

## Problemas estructurales encontrados

1. Monolito de aproximadamente 3.800 líneas con Telegram, sesiones, parsing, permisos y persistencia mezclados.
2. Proceso `claude -p` nuevo por turno; la continuidad dependía de UUID y detección manual del JSONL.
3. Lectura directa de transcripts internos para decidir `FIRST_TURN`/`RESUME`.
4. Parser y fallback de eventos mantenidos manualmente; riesgo de duplicación y exposición de campos internos.
5. Plan se reducía a `--permission-mode plan` sin canal real para `AskUserQuestion` o permisos pendientes.
6. `/stop` enviaba señales al proceso en vez de usar la interrupción oficial del SDK.
7. Estado repartido entre varios JSON y memoria de proceso.
8. Documentación, instalador y proxy pertenecían a versiones distintas.
9. No existía prueba real de dos turnos usando el mismo cliente vivo.

## Sustituciones en v3

| v2.2.1 | v3 enterprise |
|---|---|
| `subprocess.Popen` por mensaje | `ClaudeSDKClient` persistente |
| UUID + JSONL manual | session ID oficial emitido por SDK |
| parser NDJSON | mensajes tipados del SDK |
| SIGTERM/SIGKILL | `client.interrupt()` |
| Plan sin interacción | `can_use_tool` + botones/preguntas Telegram |
| JSON de estado | SQLite WAL |
| proxy mezclado | gateway directo `:8080` |
| fallback recursivo | acumulador tipado |
