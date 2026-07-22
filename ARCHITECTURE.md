# Arquitectura enterprise

## Principios

- Claude Code y su Agent SDK son la fuente de verdad para sesiones, herramientas y resultados.
- Telegram es una interfaz, no un intérprete de la TUI.
- El backend local es un gateway Anthropic-compatible configurado con `ANTHROPIC_BASE_URL`.
- Los estados operativos se guardan en SQLite; los transcripts siguen bajo control de Claude Code.
- No se reintenta automáticamente una respuesta sintética vacía.
- Las aprobaciones suspenden la misma ejecución y se resuelven por callback.

## Componentes

- `config.py`: configuración y entorno del gateway.
- `state.py`: estado durable, auditoría y métricas.
- `runtime.py`: cliente persistente, sesiones, streaming e interrupción.
- `approval.py`: permisos y `AskUserQuestion`.
- `app.py`: handlers y UX de Telegram.
- `doctor.py`: diagnóstico sin revelar secretos.
- `security.py`: allowlist, rutas, redacción y deny rules.

## Límites de confianza

- Telegram: solo IDs permitidos.
- Archivos: solo proyectos configurados y worktrees descubiertos.
- Herramientas: deny rules críticas se evalúan incluso en Bypass.
- Gateway: solo la URL local configurada.
- Git: `.env` local, nunca versionado.
