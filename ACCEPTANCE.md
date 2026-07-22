# Criterios de aceptación reales

No declarar estable hasta completar:

1. `cloky-doctor --full` sin fallos.
2. `/new` seguido de `Hola` entrega texto visible.
3. Segundo turno conserva contexto y el mismo session ID.
4. Reinicio de systemd y tercer turno reanuda la sesión.
5. `/plan` puede mostrar y resolver `AskUserQuestion`.
6. En modo Manual, una herramienta solicita aprobación por botones.
7. `/stop` interrumpe una tarea y permite iniciar otra.
8. `/sessions` lista la sesión y permite seleccionarla.
9. `/rename` cambia su título oficial.
10. `/fork` crea un ID nuevo solo en el siguiente turno.
11. Una respuesta con deltas y result se muestra una sola vez.
12. Un sentinel vacío no provoca reintento.
13. Un archivo adjunto queda dentro de `workspace/uploads`.
14. El repo no contiene `.env`, tokens, state ni logs.
15. Rollback probado desde el backup del despliegue.
