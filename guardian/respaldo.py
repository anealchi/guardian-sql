"""Esquema `guardian_backup` y SQL para guardar y restaurar filas.

Cada ejecución guarda, en la misma transacción que los cambios:

- `ejecuciones`: una fila por script aplicado (quién, cuándo, hash, texto).
- `sentencias`: una fila por UPDATE/DELETE/INSERT respaldado, con la tabla,
  su clave primaria y las columnas que asigna.
- `filas`: la imagen de cada fila en JSON. `antes` es la fila antes del
  cambio (UPDATE, DELETE) y `despues`, la fila tal y como quedó (UPDATE,
  INSERT). `despues` permite detectar, al deshacer, las filas que alguien
  ha vuelto a cambiar desde entonces.

Como todo va en la misma transacción, si el script no se confirma tampoco
queda respaldo.
"""

import hashlib
import re
import secrets
from datetime import datetime

ESQUEMA = "guardian_backup"
_ID_VALIDO = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")

SQL_CREAR = f"""
CREATE SCHEMA IF NOT EXISTS {ESQUEMA};

CREATE TABLE IF NOT EXISTS {ESQUEMA}.ejecuciones (
    id text PRIMARY KEY,
    script text NOT NULL,
    hash text NOT NULL,
    sql text NOT NULL,
    usuario text NOT NULL DEFAULT current_user,
    fecha timestamptz NOT NULL DEFAULT now(),
    estado text NOT NULL DEFAULT 'aplicado',
    sin_respaldo jsonb NOT NULL DEFAULT '[]',
    deshecho_por text,
    deshecho_en timestamptz
);

CREATE TABLE IF NOT EXISTS {ESQUEMA}.sentencias (
    ejecucion text NOT NULL
        REFERENCES {ESQUEMA}.ejecuciones ON DELETE CASCADE,
    indice integer NOT NULL,
    linea integer NOT NULL,
    operacion text NOT NULL,
    tabla text NOT NULL,
    clave text[] NOT NULL,
    columnas text[] NOT NULL DEFAULT '{{}}',
    filas integer NOT NULL DEFAULT 0,
    PRIMARY KEY (ejecucion, indice)
);

CREATE TABLE IF NOT EXISTS {ESQUEMA}.filas (
    id bigserial PRIMARY KEY,
    ejecucion text NOT NULL,
    sentencia integer NOT NULL,
    antes jsonb,
    despues jsonb,
    FOREIGN KEY (ejecucion, sentencia)
        REFERENCES {ESQUEMA}.sentencias ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS filas_sentencia
    ON {ESQUEMA}.filas (ejecucion, sentencia);

-- Origen de la ejecución cuando viene de la aplicación web (fase 3).
ALTER TABLE {ESQUEMA}.ejecuciones
    ADD COLUMN IF NOT EXISTS solicitud text,
    ADD COLUMN IF NOT EXISTS solicitado_por text,
    ADD COLUMN IF NOT EXISTS aprobado_por text;
"""


def nuevo_id():
    """Identificador legible y ordenable: 20261123-120301-a1b2."""
    return (datetime.now().strftime("%Y%m%d-%H%M%S") + "-"
            + secrets.token_hex(2))


def validar_id(ejecucion):
    if not _ID_VALIDO.match(ejecucion or ""):
        raise ValueError(f"Identificador de ejecución no válido: "
                         f"{ejecucion!r}")
    return ejecucion


def clave_bloqueo(ejecucion):
    """Clave del advisory lock que mantiene viva una ejecución.

    `ejecutar()` la toma al conectar y la conserva hasta cerrar la
    conexión, así que si otra sesión puede tomarla, la ejecución ya ha
    terminado (con COMMIT o sin él).
    """
    resumen = hashlib.sha256(
        f"guardian:{validar_id(ejecucion)}".encode("ascii")).digest()
    return int.from_bytes(resumen[:8], "big", signed=True)


def ident(nombre):
    """Nombre de columna entrecomillado para SQL."""
    return '"' + nombre.replace('"', '""') + '"'


def _filtro(ejecucion, sentencia, alias="b"):
    return (f"{alias}.ejecucion = '{validar_id(ejecucion)}' "
            f"AND {alias}.sentencia = {int(sentencia)}")


def _misma_clave(tabla_alias, registro_alias, clave):
    return " AND ".join(f"{tabla_alias}.{ident(c)} = {registro_alias}."
                        f"{ident(c)}" for c in clave)


def _sin_cambios(columnas, alias_tabla="t", alias_respaldo="b"):
    """Las columnas siguen con el valor que dejó la ejecución."""
    return " AND ".join(
        f"(to_jsonb({alias_tabla}.*) -> '{c.replace(chr(39), chr(39) * 2)}')"
        f" IS NOT DISTINCT FROM ({alias_respaldo}.despues -> "
        f"'{c.replace(chr(39), chr(39) * 2)}')" for c in columnas)


# --- Al ejecutar --------------------------------------------------------

def sql_guardar_antes(ejecucion, sentencia, tabla_sql, origen, donde, alias,
                      prefijo_with=""):
    """Copia las filas que va a tocar un UPDATE/DELETE y las bloquea.

    `FOR UPDATE` impide que otra sesión las cambie entre la copia y la
    sentencia real.
    """
    return (
        f"INSERT INTO {ESQUEMA}.filas (ejecucion, sentencia, antes) "
        f"SELECT '{validar_id(ejecucion)}', {int(sentencia)}, "
        f"to_jsonb(guardian_t.*) FROM {tabla_sql} AS guardian_t "
        f"WHERE (guardian_t.tableoid, guardian_t.ctid) IN ("
        f"{prefijo_with}SELECT {alias}.tableoid, {alias}.ctid "
        f"FROM {origen}{donde}) FOR UPDATE OF guardian_t")


def sql_guardar_despues(ejecucion, sentencia, tabla_sql, clave):
    """Tras un UPDATE, guarda cómo han quedado las filas copiadas."""
    return (
        f"UPDATE {ESQUEMA}.filas AS b SET despues = to_jsonb(t.*) "
        f"FROM {ESQUEMA}.filas AS b2 CROSS JOIN LATERAL "
        f"jsonb_populate_record(NULL::{tabla_sql}, b2.antes) AS r "
        f"JOIN {tabla_sql} AS t ON {_misma_clave('t', 'r', clave)} "
        f"WHERE b.id = b2.id AND {_filtro(ejecucion, sentencia, 'b2')}")


def sql_insertar_con_respaldo(ejecucion, sentencia, insert_sql, alias,
                              prefijo_with=""):
    """Envuelve un INSERT para guardar cada fila insertada.

    `insert_sql` es el INSERT sin WITH ni RETURNING; el WITH original va
    en `prefijo_with` y se mantiene delante.
    """
    union = ", " if prefijo_with else "WITH "
    return (
        f"{prefijo_with}{union}guardian_insertadas AS ({insert_sql} "
        f"RETURNING to_jsonb({alias}.*) AS fila) "
        f"INSERT INTO {ESQUEMA}.filas (ejecucion, sentencia, despues) "
        f"SELECT '{validar_id(ejecucion)}', {int(sentencia)}, fila "
        f"FROM guardian_insertadas")


# --- Al deshacer --------------------------------------------------------

def sql_restaurar(ejecucion, sentencia, operacion, tabla_sql, clave,
                  columnas, insertables):
    """SQL que deshace una sentencia respaldada.

    Solo toca las filas que siguen como las dejó la ejecución; las demás
    se omiten (el número de omitidas es la diferencia con `filas`).
    """
    filtro = _filtro(ejecucion, sentencia)
    if operacion == "UPDATE":
        asignaciones = ", ".join(f"{ident(c)} = r.{ident(c)}"
                                 for c in columnas)
        return (
            f"UPDATE {tabla_sql} AS t SET {asignaciones} "
            f"FROM {ESQUEMA}.filas AS b CROSS JOIN LATERAL "
            f"jsonb_populate_record(NULL::{tabla_sql}, b.antes) AS r "
            f"WHERE {filtro} AND {_misma_clave('t', 'r', clave)} "
            f"AND {_sin_cambios(columnas)}")
    if operacion == "DELETE":
        lista = ", ".join(ident(c) for c in insertables)
        valores = ", ".join(f"r.{ident(c)}" for c in insertables)
        return (
            f"INSERT INTO {tabla_sql} ({lista}) OVERRIDING SYSTEM VALUE "
            f"SELECT {valores} FROM {ESQUEMA}.filas AS b CROSS JOIN LATERAL "
            f"jsonb_populate_record(NULL::{tabla_sql}, b.antes) AS r "
            f"WHERE {filtro} ON CONFLICT DO NOTHING")
    if operacion == "INSERT":
        return (
            f"DELETE FROM {tabla_sql} AS t USING {ESQUEMA}.filas AS b "
            f"CROSS JOIN LATERAL "
            f"jsonb_populate_record(NULL::{tabla_sql}, b.despues) AS r "
            f"WHERE {filtro} AND {_misma_clave('t', 'r', clave)} "
            f"AND to_jsonb(t.*) = b.despues")
    raise ValueError(f"No se sabe deshacer {operacion}.")
