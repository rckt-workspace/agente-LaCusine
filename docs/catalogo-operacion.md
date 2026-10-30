# Operación del catálogo de productos — procedimiento (fase 5.1)

Estado: **procedimiento definido y probado en PostgreSQL local de pruebas.** No se ha importado ningún catálogo a Supabase Cloud ni a ninguna base real, y no se ha creado ningún archivo de catálogo dentro del repositorio.

## Principios

1. **El archivo original no entra al repositorio.** Vive en una carpeta local privada, fuera de `agente-LaCusine`. `.gitignore` excluye `*.xlsx`, `*.xls`, `*.xlsm`, `*.pdf` y `*.csv`, y una prueba (`tests/test_deployment_contract.py`) verifica esa exclusión.
2. **Ningún catálogo se importa automáticamente.** La importación es una orden explícita de una persona. Nada del arranque, del worker ni de una migración la ejecuta.
3. **Primero ensayo en seco, luego escritura.** Sin `--apply` el comando solo valida y muestra un resumen. No escribe.
4. **Precio de referencia y precio confirmado son cosas distintas.** Ver la sección correspondiente.

## Dónde guardar el archivo

- Carpeta local privada, por ejemplo `C:\Users\<usuario>\RCKT-privado\catalogo\` (ruta de ejemplo; elegir una fuera del repositorio).
- Ni el archivo ni su copia deben subirse a GitHub, a Render, a Supabase Storage ni a ningún servicio de terceros sin autorización.
- El procedimiento no almacena nombres de archivo, precios ni filas en el repositorio. Los resúmenes del comando contienen solo conteos y códigos de error.

## Paso 1: ensayo en seco (no escribe)

```powershell
.venv\Scripts\python.exe -m app.services.catalog_xlsx --file "C:\ruta\privada\catalogo.xlsx" --extracted-on 2026-10-29
```

- `--extracted-on` es la fecha en que se extrajo el archivo de su origen. Es obligatoria.
- Salida: JSON con `rows_total`, `valid`, `invalid`, `duplicates`, `written` (siempre `false` en el ensayo) y conteos de error por código. No imprime nombres ni precios.
- Código de salida: `0` si no hay filas inválidas ni duplicadas; `1` si las hay; `2` si el archivo no se puede leer.
- Con filas inválidas o duplicadas **no se escribe nada**, salvo que se use `--allow-partial` (ver Paso 3).

## Paso 2: base de prueba aislada

Antes de tocar cualquier base real, aplicar el catálogo en la base de prueba local para validar el resultado:

```powershell
$env:DATABASE_URL = "postgresql://postgres:<contraseña-local>@127.0.0.1:55439/lacusine_test"
.venv\Scripts\python.exe -m app.services.catalog_xlsx --file "C:\ruta\privada\catalogo.xlsx" --extracted-on 2026-10-29 --apply
```

Comprobar con consultas de solo lectura: número de filas, precios de una muestra y que una segunda importación del mismo archivo no cambia nada (`unchanged`). Esto último lo garantiza el hash de origen: solo se reescriben filas cuyo hash cambió.

Sin `DATABASE_URL`, `--apply` sale con código 2.

## Paso 3: importación parcial (solo con decisión explícita)

Por defecto, si hay filas inválidas o duplicadas, **no se escribe ni una fila**. `--allow-partial` escribe solo las filas válidas, y deja fuera las demás. Usarlo exige que una persona haya revisado el resumen de errores y acepte que el catálogo quedará incompleto. No es el modo normal.

## Paso 4: base real (solo con autorización expresa)

Aplicar en Supabase Cloud exige la autorización del responsable del proyecto y estas condiciones:

- Respaldo o punto de restauración confirmado (ver [supabase-deployment.md](supabase-deployment.md)).
- Las migraciones 1 a 11 aplicadas y verificadas en el proyecto.
- Ensayo en seco y en base de prueba sin incidencias.
- Un responsable de contenido que haya revisado una muestra de precios y disponibilidad.
- La cadena de conexión elegida según [supabase-deployment.md](supabase-deployment.md), con SSL, y un rol de importación. Nunca `service_role` ni el rol `postgres`.

## Precio de referencia y precio confirmado

- **Precio de referencia:** el que llega con la importación. Es la base de cualquier cotización, pero no es una afirmación comercial definitiva.
- **Precio confirmado:** la columna `products.price_confirmed_at` registra cuándo una persona confirmó el precio. Un precio confirmado exige que haya precio (restricción en la base).
- **Hoy no existe un flujo que establezca esa marca.** Ningún código de la aplicación la escribe. Es un paso manual, pendiente de decisión del equipo.
- Mientras el precio no esté confirmado, el agente debe presentarlo como referencia, no como precio cerrado. Esa regla debe revisarse antes del piloto.

## Vocabulario de búsqueda

- El vocabulario de búsqueda vive en memoria, por proceso. **Tras reimportar el catálogo hay que reiniciar el proceso que lo usa** (API y worker) para que tome los nuevos términos. Así lo registra la limitación 5 de [worker.md](worker.md).

## Recuperación

- **Reimportar el mismo archivo** es seguro: no altera nada.
- **Reimportar un archivo corregido** actualiza solo las filas cuyo hash cambió.
- **La importación no borra productos.** Un producto que desaparece del archivo nuevo **permanece** en la base con sus datos anteriores. Esto es una limitación conocida. Para retirarlo hay que hacerlo con una acción explícita y auditada, no con la importación.
- **Si la importación fue incorrecta:** restaurar desde el respaldo anterior a la operación, no corregir a mano fila por fila. Luego volver a ejecutar el ensayo con el archivo corregido.

## Qué no cubre este procedimiento

- Validación comercial de precios, descripciones y disponibilidad. Corresponde al responsable de contenido de La Cuisine.
- Ingesta automática, programada o desde un servicio externo. No existe y no se propone en esta fase.
- Eliminación automática de productos retirados.
- Inclusión del archivo original en el repositorio, en la imagen de contenedor o en Render.

## Productos descontinuados (decisión pendiente)

Estado: **no implementado.** La importación no distingue un producto retirado de uno vigente. Un producto que desaparece del archivo permanece en la base, como se describe arriba.

Regla propuesta, pendiente de aprobación comercial:
- Una importación **completa** que no incluye un producto lo marca como ausente (`missing_since`), sin borrarlo.
- Un producto ausente no se recomienda en las respuestas del agente, pero sigue visible para la administración.
- Si vuelve a aparecer en una importación, se limpia la marca.
- Solo una persona autorizada puede retirarlo definitivamente, con motivo y auditoría.

No se eliminan productos automáticamente. Hasta aprobar la regla, el agente puede citar un producto que ya no se vende: esa limitación debe comunicarse al responsable de contenido.
