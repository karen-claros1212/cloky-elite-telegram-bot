# Changelog

## 3.0.0-rc1 — 2026-07-21

- Sustituye el parser manual de `stream-json` por Claude Agent SDK.
- Usa `ClaudeSDKClient` persistente para chat multi-turno.
- Sesiones oficiales con `list_sessions`, `get_session_messages` y `rename_session`.
- Aprobaciones y preguntas interactivas vía `can_use_tool`.
- Modos dinámicos mediante `set_permission_mode`.
- Interrupción mediante `client.interrupt()`.
- Streaming con buffers separados; no duplica delta, assistant y result.
- SQLite para estado, métricas y auditoría.
- Elimina proxy y edición de JSONL.
- Migración desde estado JSON de v2.2.1.
