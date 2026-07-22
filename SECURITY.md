# Seguridad

- El repositorio nunca debe contener `.env`.
- Solo usuarios de `ALLOWED_TELEGRAM_USER_ID` pueden operar.
- Los proyectos se limitan mediante `ALLOWED_PROJECTS`.
- Las reglas `disallowed_tools` bloquean operaciones críticas incluso en Bypass.
- Los argumentos de herramientas se muestran redacted y truncados.
- Los logs no registran prompts completos, tokens ni contenido de archivos.
- `bypassPermissions` concede control amplio; úselo solo en una máquina y proyectos confiables.
