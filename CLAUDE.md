# CLAUDE.md

Este archivo sirve de guía a Claude Code para trabajar con este repositorio.

## Descripción general

SQL Guardian (`guardian/`) revisa scripts SQL de PostgreSQL antes de
ejecutarlos en producción. El código, los comentarios, los mensajes y la
documentación están en **español**; el código nuevo debe seguir igual. El
plan completo (fases F1–F5) está en el README; están hechas F1, F2 y F3.

## Comandos

```
.venv\Scripts\activate
pip install -e .[desarrollo]
guardian analizar script.sql [--dsn ...] [--simular] [-m informe.md]
guardian ejecutar script.sql [--ignorar-tabla T] [--confirmar]
guardian deshacer ID [--solo-sql] [--confirmar]
guardian historial
guardian web [--config guardian.toml]   # aplicación web (F3)
guardian usuario crear NOMBRE --rol admin
python herramientas/pg_local.py iniciar|parar|dsn
pytest
flake8 guardian tests herramientas
```

Todo debe pasar flake8 con la configuración por defecto (79 caracteres).

## Arquitectura

- `sentencias.py`: `leer_script()` divide el script con pglast (parser real
  de PostgreSQL) en `Sentencia` con tipo, operación, tablas escritas y
  columnas. Las posiciones de pglast son en caracteres, no en bytes, y
  `stmt_location` incluye los comentarios previos: se saltan con
  `_INICIO_IGNORABLE`.
- `analisis.py`: reglas estáticas → `Hallazgo(nivel, codigo, mensaje,
  linea)`. Los avisos informativos repetidos se agrupan en uno por script.
- `esquema.py`: lee de `pg_catalog` clave primaria, triggers (decodifica
  `tgtype` y `tgattr` para `UPDATE OF`), FKs entrantes y filas estimadas.
- `impacto.py`: `calcular()` abre la conexión, lee el esquema y ejecuta uno
  de los dos modos. **Los dos terminan siempre con `conexion.rollback()` en
  un `finally`**; no romper esa garantía.
  - `contar()`: solo ejecuta `SET` y sentencias cuyas tablas escritas son
    todas temporales creadas por el propio script. UPDATE/DELETE/INSERT se
    convierten en `SELECT count(*)` con `consulta_de_conteo()` (con
    FROM/USING se cuenta `DISTINCT (tableoid, ctid)`). Cada sentencia va en
    un SAVEPOINT para que un error no aborte el resto; los SAVEPOINT se
    lanzan por otro cursor para no perder el `rowcount`.
  - `simular()`: ejecuta todo salvo las sentencias de transacción del
    script y mide los cambios por tabla con `pg_stat_xact_user_tables`
    (antes/después de cada sentencia), lo que revela los triggers.
- `respaldo.py`: DDL de `guardian_backup` (ejecuciones, sentencias, filas
  con `antes`/`despues` en jsonb) y generadores de SQL. Los valores que se
  incrustan en el SQL son el id validado con `validar_id()`, enteros y
  nombres entrecomillados con `ident()` o con `format('%I.%I')`: no pasar
  parámetros `%s` en SQL que contenga texto del usuario (psycopg
  interpretaría sus `%`).
- `ejecucion.py`: `comprobar()` decide qué se puede respaldar;
  `ejecutar()` lo hace todo en una transacción y solo hace COMMIT si
  `confirmar(ejecucion)` devuelve True. Los cambios en otras tablas se
  detectan con `pg_stat_xact_user_tables`, pero TRUNCATE y DDL no salen ahí:
  por eso los pasos forzados se marcan con `forzada` sin mirar las
  estadísticas.
- `deshacer.py`: `deshacer()` en orden inverso de sentencias, con
  comprobación de que cada fila sigue como la dejó la ejecución;
  `script_deshacer()` y `historial()`.
- `informe.py`: texto de consola y Markdown. `cli.py`: argparse; códigos de
  salida 0/1/2. La pregunta de confirmación usa un hilo con tiempo límite
  (`pedir_confirmacion`); en Windows, `< NUL` cuenta como terminal, pero
  `input()` recibe EOF y se hace ROLLBACK.

### Aplicación web (`guardian/web/`)

- `config.py`: lee `guardian.toml` (ver `guardian.ejemplo.toml`). Los DSN
  nunca salen del servidor: `Conexion.publica()` es lo único que ve el
  navegador.
- `almacen.py`: SQLite (usuarios, sesiones, solicitudes, eventos). Cada
  operación abre conexión con `BEGIN IMMEDIATE` (`transaccion()`); el
  esquema se crea fuera porque `executescript` confirma por su cuenta. Los
  eventos forman una cadena de hashes (`verificar_auditoria`) y hay
  triggers que impiden UPDATE/DELETE sobre ellos.
- `servicio.py`: reglas del flujo, sin HTTP (ver el diagrama del
  docstring). Las transiciones se hacen con `UPDATE ... WHERE estado IN
  (...)` para que dos peticiones a la vez no ejecuten dos veces. Al
  ejecutar, `confirmar()` compara las filas reales con `filas_aprobadas()`.
  `ejecutar()` toma un advisory lock de sesión (`clave_bloqueo(id)`)
  **antes** de pasar la solicitud a 'ejecutando' y guardar su
  `ejecucion_id`; `recuperar()` (al arrancar y con el botón del detalle)
  usa `estado_en_base()`: si el bloqueo está libre, la ejecución terminó, y
  la fila de `guardian_backup.ejecuciones` dice si hubo COMMIT.
- `app.py`: FastAPI. Middleware que exige `X-Guardian: 1` en lo que no es
  GET y añade CSP estricta (por eso `estatico/` no puede tener scripts ni
  estilos en línea).
- `estatico/`: HTML/CSS/JS sin compilación. `app.js` construye todo con
  `h()` y `textContent`: no usar `innerHTML` con datos. En el detalle, cada
  nota del margen ocupa con `rowspan` las líneas de su sentencia
  (`rangosDeSentencias`). `estilos.css` solo usa sus escalas (`--e-*`
  espaciado, `--t-*` tamaños, pesos 400/500/600/700, `--radio` y
  `--radio-chico`); las tablas van dentro de `.tabla-contenedor` para que
  la página no se desplace en horizontal en móvil (comprobado a 390 px).
- Las conexiones a PostgreSQL pasan por `impacto.conectar()`, que pone
  `connect_timeout=10` si la cadena no lo trae (sin él, un servidor caído
  deja la petición colgada minutos en Windows).

## Pruebas

- `tests/datos/esquema_prueba.sql`: esquema sintético de mantenimiento
  (parte_trabajo, orden_trabajo_tarea, tarea_planificacion...) con dos
  triggers y datos inventados con duplicados. Los números esperados de las
  pruebas dependen de esos datos: si se cambian, revisar `test_impacto.py`.
- `test_web.py` usa el `TestClient` de FastAPI con una configuración que
  apunta a la base de pruebas; baja `ITERACIONES` de PBKDF2 para ir rápido.
- `test_impacto.py` usa los scripts reales de `../operaciones-sql-produccion`
  (se salta si no están) y comprueba que ningún modo cambia la base
  (huella md5 antes/después).
- El PostgreSQL portátil vive en `.pg/` (ignorada por git). En Windows,
  `pg_ctl start` no debe lanzarse con la salida capturada: el servidor
  hereda los descriptores y `subprocess.run` no termina nunca.
