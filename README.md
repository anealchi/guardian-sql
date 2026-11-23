# SQL Guardian

Revisa scripts SQL de PostgreSQL **antes** de ejecutarlos en producción: qué
tablas tocan, cuántas filas cambia cada sentencia, qué triggers se disparan y
qué riesgos tienen (COMMIT al final, `UPDATE` sin `WHERE`, `DROP` peligrosos...).
Todo funciona en local: el script y los datos no salen de tu equipo.

Fases hechas: **F1** (análisis estático, lectura del esquema e impacto) y
**F2** (ejecución con respaldo y `deshacer`). Las siguientes (interfaz web con
aprobaciones, explicación con IA local) están en el plan del proyecto.

## Instalación

```
python -m venv .venv
.venv\Scripts\activate
pip install -e .[desarrollo]
```

## Uso

```
# Solo análisis estático (no necesita base de datos)
guardian analizar script.sql

# Con conexión: lee el esquema y cuenta las filas afectadas (modo conteo)
set GUARDIAN_DSN=postgresql://usuario@host:5432/base
guardian analizar script.sql

# Simulación: ejecuta el script entero y lo deshace con ROLLBACK
guardian analizar script.sql --simular

# Guardar también el informe en Markdown
guardian analizar script.sql -m informe.md
```

Códigos de salida: `0` sin riesgos altos, `1` hay riesgos altos o críticos,
`2` error (sintaxis, conexión...). Sirve para usarlo en CI.

### Ejecutar con respaldo y deshacer

```
guardian ejecutar script.sql                      # pide escribir COMMIT
guardian ejecutar script.sql --ignorar-tabla parte_trabajo_tarea_log
guardian historial
guardian deshacer 20261123-123609-4a03 --solo-sql # ver el SQL, sin ejecutar
guardian deshacer 20261123-123609-4a03            # pide escribir COMMIT
```

`ejecutar` hace todo el script en **una sola transacción** (se ignoran los
BEGIN/COMMIT/ROLLBACK del propio script):

1. Antes de cada UPDATE/DELETE copia las filas que va a tocar en
   `guardian_backup.filas` (en JSON) y las bloquea con `FOR UPDATE`; cada
   INSERT se envuelve para guardar las filas insertadas. Si una sentencia
   cambia un número de filas distinto del respaldado (otra sesión ha
   cambiado datos a la vez), se deshace todo.
2. Muestra cuántas filas ha cambiado cada sentencia y lo que han cambiado
   los triggers y las cascadas.
3. Solo hace COMMIT si escribes `COMMIT` antes de `--tiempo-confirmacion`
   (120 s por defecto; mientras tanto las filas siguen bloqueadas). Con
   `--confirmar` no pregunta. Si la entrada no es interactiva, hace
   ROLLBACK. El respaldo se guarda en la misma transacción: si no se
   confirma, no queda nada.

Se niega a ejecutar (salvo con `--forzar`, y entonces esas sentencias van
sin respaldo) cuando hay riesgos críticos o sentencias que no se pueden
respaldar: DDL sobre tablas permanentes, `TRUNCATE`, `MERGE`, `DO`/`CALL`,
tablas sin clave primaria, vistas, UPDATE que cambian la clave primaria e
`INSERT ... ON CONFLICT DO UPDATE`.

Los cambios que hacen los triggers o las claves foráneas en **otras** tablas
no tienen respaldo. Si los hay, no deja confirmar salvo con
`--permitir-indirectos`, o con `--ignorar-tabla` para tablas cuyos cambios
no importan (logs). Quedan anotados en la ejecución y `deshacer` los lista
como no reversibles.

`deshacer` restaura las sentencias en orden inverso y **solo toca las filas
que siguen como las dejó la ejecución**: si alguien ha cambiado una fila
después, se omite y se avisa. En los UPDATE restaura las columnas que
asignaba la sentencia; las que cambie un trigger `BEFORE` no se tocan.
`--solo-sql` muestra el script equivalente (acaba en ROLLBACK) para
revisarlo o ejecutarlo a mano.

Un `DROP TABLE` de una tabla temporal del script se ejecuta como
`DROP TABLE pg_temp.nombre`, para no borrar nunca una tabla permanente con
el mismo nombre.

El usuario de la conexión necesita permiso para crear el esquema
`guardian_backup` (o que alguien lo cree antes con `CREATE SCHEMA`).

### Modos de impacto

Los dos modos terminan **siempre con ROLLBACK**, aunque el script diga COMMIT.

| | Conteo (por defecto) | Simulación (`--simular`) |
|---|---|---|
| Qué ejecuta | Las tablas temporales del script y un `SELECT count(*)` por cada UPDATE/DELETE/INSERT | El script completo |
| Bloqueos | Ninguno en tablas permanentes | Bloquea las filas que modifica mientras dura |
| Precisión | Exacta salvo cuando una sentencia depende de otra anterior (se marca con `~`) | Exacta |
| Triggers | Indica cuáles se dispararían | Mide lo que cambian de verdad en cada tabla |
| Dónde usarlo | Producción | Staging o copia de producción; en producción con cuidado (`--lock-timeout`) |

Límites: el modo simulación deshace todo lo que ocurre dentro de la base de
datos, pero no lo que una función haga fuera de ella (dblink, `COPY ... TO
PROGRAM`, extensiones que llamen a servicios externos), ni los saltos de las
secuencias (`nextval`). El modo conteo nunca ejecuta `DO`, `CALL`, `COPY` ni
DDL sobre tablas permanentes.

## Reglas

| Código | Nivel | Qué detecta |
|--------|-------|-------------|
| DML001 | crítico | `UPDATE`/`DELETE` sin `WHERE` |
| DML003 | crítico | `TRUNCATE` de una tabla permanente |
| DDL001 | crítico | `DROP` de una tabla u otro objeto permanente |
| TX001 | alto | El script no empieza con `BEGIN` |
| TX002 | alto | El script termina con `COMMIT` |
| TX004 | alto | `COMMIT` a mitad del script |
| DDL003 | alto | `ALTER TABLE` que bloquea la tabla |
| GEN002 | alto | Escrituras escondidas en un `WITH` |
| ESQ001 | alto | La tabla no existe en la base de datos |
| ESQ003 | alto | `DELETE` que se propaga por claves foráneas en cascada |
| TX003 | medio | Cambios después del último `COMMIT`/`ROLLBACK` |
| TX005 | medio | Varias transacciones en el mismo script |
| DDL002 | medio | `DROP TABLE tmp` sin `pg_temp.`: podría borrar una tabla permanente |
| DDL004 | medio | `CREATE INDEX` sin `CONCURRENTLY` |
| DML002 | medio | Borrado físico (en vez de lógico) |
| ESQ002 | medio | Triggers que se disparan con la sentencia |
| SIM001 | medio | La simulación cambió otras tablas (triggers/cascadas) |
| DML004, DML005, GEN001, GEN003 | info | Borrado lógico, usuario fijo, fechas fijas, `SET` sin `LOCAL` |

## Pruebas

```
python herramientas/pg_local.py iniciar   # PostgreSQL portátil en .pg/
pytest
flake8 guardian tests herramientas
```

Las pruebas usan `GUARDIAN_TEST_DSN` si está definida; si no, el PostgreSQL
portátil de `.pg/pgsql` (binarios oficiales de EnterpriseDB descomprimidos ahí).
Si no hay ninguno, las pruebas con base de datos se saltan.
