# Procedimiento de despliegue por SQL Editor (superado)

Este borrador fue sustituido. El procedimiento vigente, con el diagnóstico de solo lectura, el script transaccional validado y los criterios GO/NO-GO, está en:

- [docs/despliegue/README.md](despliegue/README.md)
- [docs/despliegue/00-diagnostico-solo-lectura.sql](despliegue/00-diagnostico-solo-lectura.sql)
- [docs/despliegue/despliegue-16-18-sql-editor.sql](despliegue/despliegue-16-18-sql-editor.sql)

Motivo del cambio: el borrador proponía registrar el historial con filas escritas a mano, sin comprobar su estructura real. El procedimiento vigente verifica la estructura y usa filas generadas por la propia CLI.
