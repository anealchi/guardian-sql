"""Deshacer una ejecución a partir de su respaldo en `guardian_backup`.

Las sentencias se deshacen en orden inverso: así, si dos sentencias del
script tocaron la misma fila, se vuelve paso a paso al valor original.
Solo se restauran las filas que siguen como las dejó la ejecución; las que
alguien ha cambiado después se omiten y se informa de cuántas son.
"""

from dataclasses import dataclass, field

import psycopg

from guardian.impacto import (
    diferencia, estadisticas, mensaje_error, preparar_transaccion,
)
from guardian.respaldo import ESQUEMA, sql_restaurar, validar_id

_SQL_TABLA = """
select format('%%I.%%I', n.nspname, c.relname),
    coalesce((
        select array_agg(a.attname order by a.attnum)
        from pg_attribute a
        where a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
            and a.attgenerated = ''
    ), '{}')
from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
where c.oid = to_regclass(%s)
"""


class ErrorDeshacer(Exception):
    pass


@dataclass
class PasoDeshecho:
    indice: int
    linea: int
    operacion: str
    tabla: str
    respaldadas: int
    restauradas: int | None = None
    sql: str = ""
    # Cambios en otras tablas al deshacer (triggers).
    indirectos: dict = field(default_factory=dict)

    @property
    def omitidas(self):
        if self.restauradas is None:
            return 0
        return self.respaldadas - self.restauradas


@dataclass
class Deshacer:
    id: str
    script: str = ""
    usuario: str = ""
    fecha: str = ""
    pasos: list = field(default_factory=list)
    sin_respaldo: list = field(default_factory=list)
    aplicado: bool = False
    motivo: str = ""

    @property
    def omitidas(self):
        return sum(p.omitidas for p in self.pasos)


def _leer_ejecucion(conexion, ejecucion_id):
    fila = conexion.execute(
        f"SELECT script, usuario, fecha::text, estado, sin_respaldo "
        f"FROM {ESQUEMA}.ejecuciones WHERE id = %s",
        (ejecucion_id,)).fetchone()
    if fila is None:
        raise ErrorDeshacer(f"No existe la ejecución {ejecucion_id}.")
    script, usuario, fecha, estado, sin_respaldo = fila
    if estado != "aplicado":
        raise ErrorDeshacer(f"La ejecución {ejecucion_id} está en estado "
                            f"'{estado}': no se puede deshacer.")
    resultado = Deshacer(id=ejecucion_id, script=script, usuario=usuario,
                         fecha=fecha, sin_respaldo=sin_respaldo or [])

    filas = conexion.execute(
        f"SELECT indice, linea, operacion, tabla, clave, columnas, filas "
        f"FROM {ESQUEMA}.sentencias WHERE ejecucion = %s "
        f"ORDER BY indice DESC", (ejecucion_id,)).fetchall()
    for indice, linea, operacion, tabla, clave, columnas, n in filas:
        # El nombre se vuelve a resolver en el catálogo: no se usa tal
        # cual el texto guardado.
        datos = conexion.execute(_SQL_TABLA, (tabla,)).fetchone()
        if datos is None:
            raise ErrorDeshacer(f"La tabla {tabla} ya no existe.")
        tabla_sql, insertables = datos
        paso = PasoDeshecho(indice, linea, operacion, tabla_sql, n)
        paso.sql = sql_restaurar(ejecucion_id, indice, operacion, tabla_sql,
                                 clave, columnas, insertables)
        resultado.pasos.append(paso)
    return resultado


def script_deshacer(dsn, ejecucion_id):
    """SQL completo para deshacer a mano (termina con ROLLBACK)."""
    validar_id(ejecucion_id)
    with psycopg.connect(dsn) as conexion:
        datos = _leer_ejecucion(conexion, ejecucion_id)
        conexion.rollback()
    partes = [f"-- Deshacer la ejecución {datos.id} ({datos.script}, "
              f"{datos.usuario}, {datos.fecha}).",
              "-- Solo restaura las filas que siguen como las dejó la "
              "ejecución.",
              "BEGIN;", ""]
    for paso in datos.pasos:
        partes += [f"-- Sentencia {paso.indice} (línea {paso.linea}): "
                   f"{paso.operacion} {paso.tabla}, {paso.respaldadas} "
                   "filas respaldadas.", paso.sql + ";", ""]
    partes += [f"UPDATE {ESQUEMA}.ejecuciones SET estado = 'deshecho', "
               f"deshecho_por = current_user, deshecho_en = now() "
               f"WHERE id = '{datos.id}';", "",
               "-- Cambiar por COMMIT para aplicar.", "ROLLBACK;"]
    return "\n".join(partes) + "\n"


def deshacer(dsn, ejecucion_id, confirmar, statement_timeout="60s",
             lock_timeout="3s", tiempo_confirmacion=120):
    """Deshace una ejecución. `confirmar(resultado)` decide el COMMIT."""
    validar_id(ejecucion_id)
    resultado = None
    with psycopg.connect(dsn, autocommit=False) as conexion:
        try:
            preparar_transaccion(conexion, statement_timeout, lock_timeout)
            conexion.execute(
                "SELECT set_config('idle_in_transaction_session_timeout', "
                "%s, true)", (f"{int(tiempo_confirmacion) + 30}s",))
            resultado = _leer_ejecucion(conexion, ejecucion_id)
            # Bloquea la ejecución para que nadie la deshaga a la vez.
            conexion.execute(f"SELECT 1 FROM {ESQUEMA}.ejecuciones "
                             f"WHERE id = %s FOR UPDATE", (ejecucion_id,))
            antes = estadisticas(conexion)
            with conexion.cursor() as cur:
                for paso in resultado.pasos:
                    try:
                        cur.execute(paso.sql)
                    except psycopg.Error as error:
                        raise ErrorDeshacer(
                            f"Sentencia {paso.indice} (línea {paso.linea}): "
                            f"{mensaje_error(error)}")
                    paso.restauradas = cur.rowcount
                    despues = estadisticas(conexion)
                    efectos = diferencia(antes, despues)
                    antes = despues
                    corto = paso.tabla.replace('"', "")
                    paso.indirectos = {
                        t: v for t, v in efectos.items()
                        if t != corto and not t.startswith(ESQUEMA + ".")}
            conexion.execute(
                f"UPDATE {ESQUEMA}.ejecuciones SET estado = 'deshecho', "
                f"deshecho_por = current_user, deshecho_en = now() "
                f"WHERE id = %s", (ejecucion_id,))
            if confirmar(resultado):
                conexion.commit()
                resultado.aplicado = True
            else:
                resultado.motivo = "No se confirmó: se ha hecho ROLLBACK."
        finally:
            if resultado is None or not resultado.aplicado:
                conexion.rollback()
    return resultado


def historial(dsn, limite=20):
    """Últimas ejecuciones registradas (lista de diccionarios)."""
    with psycopg.connect(dsn) as conexion:
        existe = conexion.execute(
            "SELECT to_regclass(%s) IS NOT NULL",
            (f"{ESQUEMA}.ejecuciones",)).fetchone()[0]
        if not existe:
            return []
        cur = conexion.execute(
            f"SELECT e.id, e.fecha, e.usuario, e.script, e.estado, "
            f"coalesce(sum(s.filas), 0)::int AS filas, "
            f"jsonb_array_length(e.sin_respaldo) AS sin_respaldo "
            f"FROM {ESQUEMA}.ejecuciones e "
            f"LEFT JOIN {ESQUEMA}.sentencias s ON s.ejecucion = e.id "
            f"GROUP BY e.id ORDER BY e.fecha DESC LIMIT %s", (limite,))
        columnas = [d.name for d in cur.description]
        return [dict(zip(columnas, f)) for f in cur.fetchall()]
