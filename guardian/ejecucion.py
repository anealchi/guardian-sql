"""Ejecución real de un script con respaldo para poder deshacerlo.

Todo el script va en **una sola transacción** controlada por Guardian (se
ignoran los BEGIN/COMMIT/ROLLBACK del propio script):

1. Comprueba que cada sentencia se puede respaldar. Si alguna no (DDL sobre
   tablas permanentes, TRUNCATE, MERGE, DO, tablas sin clave primaria...),
   se niega salvo con `forzar`.
2. Antes de cada UPDATE/DELETE copia las filas afectadas en
   `guardian_backup.filas` y las bloquea; cada INSERT se envuelve para
   guardar las filas insertadas.
3. Mide con `pg_stat_xact_user_tables` lo que cambia cada sentencia. Los
   cambios que no están respaldados (triggers, claves foráneas en cascada)
   impiden confirmar salvo con `permitir_indirectos` o si la tabla está en
   `ignorar` (p. ej. tablas de log).
4. Llama a `confirmar(ejecucion)`. Solo si devuelve True se hace COMMIT;
   en cualquier otro caso, ROLLBACK.
"""

import hashlib
import json
from dataclasses import dataclass, field

import psycopg
from pglast import ast
from pglast.enums import OnConflictAction

from guardian.analisis import CRITICO, analizar
from guardian.esquema import leer_tablas
from guardian.impacto import (
    FUERA_DE_TRANSACCION, diferencia, estadisticas, mensaje_error,
    origen_de_modificacion, preparar_transaccion, prefijo_with,
)
from guardian.respaldo import (
    ESQUEMA, SQL_CREAR, nuevo_id, sql_guardar_antes, sql_guardar_despues,
    sql_insertar_con_respaldo,
)
from guardian.sentencias import (
    CONFIGURACION, CONSULTA, DDL, MODIFICACION, TRANSACCION, deparsear,
    modifica_dentro, tablas_permanentes_escritas, tablas_temporales,
)

# Qué cambio se espera en la tabla objetivo: (insertadas, actualizadas,
# borradas) en función de las filas afectadas.
_ESPERADO = {
    "UPDATE": lambda n: (0, n, 0),
    "DELETE": lambda n: (0, 0, n),
    "INSERT": lambda n: (n, 0, 0),
}


class ErrorEjecucion(Exception):
    """Una sentencia falló: se ha hecho ROLLBACK de todo."""

    def __init__(self, linea, mensaje):
        super().__init__(f"Línea {linea}: {mensaje}")
        self.linea = linea


@dataclass
class Bloqueo:
    linea: int
    mensaje: str


@dataclass
class Paso:
    indice: int
    linea: int
    operacion: str
    tabla: str | None
    filas: int | None
    respaldadas: int | None = None
    # {tabla: (insertadas, actualizadas, borradas)} que no cubre el respaldo.
    indirectos: dict = field(default_factory=dict)
    ignorados: dict = field(default_factory=dict)
    # Ejecutada con --forzar sin respaldo. Se marca siempre, porque algunos
    # cambios (TRUNCATE, DDL) no aparecen en las estadísticas.
    forzada: bool = False
    nota: str = ""


@dataclass
class Ejecucion:
    id: str
    script: str
    pasos: list = field(default_factory=list)
    bloqueos: list = field(default_factory=list)
    hallazgos: list = field(default_factory=list)
    aplicada: bool = False
    motivo: str = ""

    @property
    def sin_respaldo(self):
        """Pasos con cambios que no se podrán deshacer automáticamente."""
        return [p for p in self.pasos if p.indirectos or p.forzada]

    @property
    def filas_respaldadas(self):
        return sum(p.respaldadas or 0 for p in self.pasos)


def _solo_temporales(s, temporales):
    return (s.tipo in (MODIFICACION, DDL) and bool(s.tablas_escritas)
            and all(t in temporales for t in s.tablas_escritas))


def _motivo_sin_respaldo(s, tabla):
    """None si la sentencia se puede respaldar; si no, el motivo."""
    if s.operacion not in _ESPERADO:
        return f"{s.operacion} no se puede respaldar."
    if modifica_dentro(s.nodo):
        return "modifica datos dentro de un WITH."
    if tabla is None or not tabla.existe:
        return f"la tabla {s.tablas_escritas[0]} no existe."
    if tabla.es_vista:
        return f"{tabla.nombre} es una vista."
    if not tabla.clave_primaria:
        return f"{tabla.nombre} no tiene clave primaria."
    if s.operacion == "UPDATE" and set(s.columnas) & set(
            tabla.clave_primaria):
        return "el UPDATE cambia la clave primaria."
    conflicto = getattr(s.nodo, "onConflictClause", None)
    if conflicto is not None and \
            conflicto.action == OnConflictAction.ONCONFLICT_UPDATE:
        return "INSERT ... ON CONFLICT DO UPDATE no se puede respaldar."
    return None


def comprobar(sentencias, tablas):
    """Sentencias que impiden ejecutar con respaldo completo."""
    temporales = tablas_temporales(sentencias)
    bloqueos = []
    for s in sentencias:
        if isinstance(s.nodo, FUERA_DE_TRANSACCION) or (
                isinstance(s.nodo, ast.IndexStmt) and s.nodo.concurrent):
            bloqueos.append(Bloqueo(s.linea, f"{s.operacion} no se puede "
                                    "ejecutar dentro de una transacción."))
        elif s.tipo in (TRANSACCION, CONFIGURACION):
            continue
        elif s.tipo == CONSULTA:
            if modifica_dentro(s.nodo):
                bloqueos.append(Bloqueo(
                    s.linea, "La consulta modifica datos dentro de un WITH: "
                    "no se puede respaldar."))
        elif _solo_temporales(s, temporales):
            continue
        else:
            tabla = (tablas.get(s.tablas_escritas[0])
                     if s.tablas_escritas else None)
            motivo = _motivo_sin_respaldo(s, tabla)
            if motivo:
                bloqueos.append(Bloqueo(s.linea, motivo[0].upper()
                                        + motivo[1:]))
    return bloqueos


def _texto_drop_temporal(s):
    """DROP de temporales siempre con pg_temp, para no tocar permanentes."""
    nodo = s.nodo
    nombres = ", ".join(f"pg_temp.{t}" for t in s.tablas_escritas)
    si_existe = "IF EXISTS " if nodo.missing_ok else ""
    return f"DROP TABLE {si_existe}{nombres}"


def _insert_para_respaldo(nodo):
    """Deparsea un INSERT sin WITH ni RETURNING y con alias en la tabla.

    Devuelve (sql, alias). El alias hace falta para el RETURNING.
    """
    originales = (nodo.withClause, nodo.returningClause, nodo.relation.alias)
    nodo.withClause = None
    nodo.returningClause = None
    if nodo.relation.alias is None:
        nodo.relation.alias = ast.Alias(aliasname="guardian_fila")
    try:
        return deparsear(nodo), nodo.relation.alias.aliasname
    finally:
        (nodo.withClause, nodo.returningClause,
         nodo.relation.alias) = originales


def _contar_respaldo(conexion, ejecucion, indice):
    return conexion.execute(
        f"SELECT count(*) FROM {ESQUEMA}.filas "
        f"WHERE ejecucion = %s AND sentencia = %s",
        (ejecucion, indice)).fetchone()[0]


def _registrar_sentencia(conexion, ejecucion, s, tabla):
    conexion.execute(
        f"INSERT INTO {ESQUEMA}.sentencias (ejecucion, indice, linea, "
        f"operacion, tabla, clave, columnas) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (ejecucion, s.indice, s.linea, s.operacion, tabla.nombre_sql,
         tabla.clave_primaria, s.columnas))


def _ejecutar_con_respaldo(conexion, cur, ejecucion, s, tabla):
    """Ejecuta una sentencia respaldada; devuelve (filas, respaldadas)."""
    _registrar_sentencia(conexion, ejecucion, s, tabla)
    nodo = s.nodo
    if s.operacion in ("UPDATE", "DELETE"):
        origen, donde, alias, _ = origen_de_modificacion(nodo)
        conexion.execute(sql_guardar_antes(
            ejecucion, s.indice, tabla.nombre_sql, origen, donde, alias,
            prefijo_with(nodo)))
        respaldadas = _contar_respaldo(conexion, ejecucion, s.indice)
        cur.execute(s.texto)
        filas = cur.rowcount
        if filas != respaldadas:
            raise ErrorEjecucion(
                s.linea, f"la sentencia cambió {filas} filas pero se "
                f"respaldaron {respaldadas}: otra sesión ha cambiado los "
                "datos a la vez. Vuelve a intentarlo.")
        if s.operacion == "UPDATE":
            conexion.execute(sql_guardar_despues(
                ejecucion, s.indice, tabla.nombre_sql,
                tabla.clave_primaria))
    else:
        insert_sql, alias = _insert_para_respaldo(nodo)
        cur.execute(sql_insertar_con_respaldo(
            ejecucion, s.indice, insert_sql, alias, prefijo_with(nodo)))
        filas = respaldadas = cur.rowcount
    conexion.execute(
        f"UPDATE {ESQUEMA}.sentencias SET filas = %s "
        f"WHERE ejecucion = %s AND indice = %s",
        (respaldadas, ejecucion, s.indice))
    return filas, respaldadas


def _coincide(tabla, nombres):
    corto = tabla.split(".", 1)[-1]
    return tabla in nombres or corto in nombres


def _repartir_efectos(paso, efectos, ignorar):
    """Separa lo que cubre el respaldo de los cambios indirectos."""
    efectos = {t: v for t, v in efectos.items()
               if not t.startswith(ESQUEMA + ".")}
    objetivo = None
    if paso.tabla and paso.respaldadas is not None:
        objetivo = next((t for t in efectos
                         if _coincide(t, {paso.tabla})), None)
    for tabla, valores in efectos.items():
        if tabla == objetivo:
            esperado = _ESPERADO[paso.operacion](paso.respaldadas)
            valores = tuple(max(v - e, 0) for v, e in zip(valores, esperado))
            if not any(valores):
                continue
        destino = paso.ignorados if _coincide(tabla, ignorar) \
            else paso.indirectos
        destino[tabla] = valores


def _resumen_sin_respaldo(ejecucion):
    return [{"linea": p.linea, "operacion": p.operacion, "tabla": p.tabla,
             "forzada": p.forzada,
             "cambios": {t: list(v) for t, v in p.indirectos.items()}}
            for p in ejecucion.sin_respaldo]


def ejecutar(dsn, nombre_script, fuente, sentencias, confirmar,
             forzar=False, permitir_indirectos=False, ignorar=(),
             statement_timeout="60s", lock_timeout="3s",
             tiempo_confirmacion=120):
    """Ejecuta el script con respaldo. Devuelve una `Ejecucion`.

    `confirmar(ejecucion)` se llama con la transacción abierta y decide si
    se hace COMMIT. Lanza `ErrorEjecucion` si una sentencia falla.
    """
    ejecucion = Ejecucion(id=nuevo_id(), script=nombre_script)
    ejecucion.hallazgos = analizar(sentencias)
    criticos = [h for h in ejecucion.hallazgos if h.nivel == CRITICO]
    ignorar = set(ignorar)
    temporales = tablas_temporales(sentencias)

    with psycopg.connect(dsn, autocommit=False) as conexion:
        tablas = leer_tablas(conexion, tablas_permanentes_escritas(
            sentencias))
        conexion.rollback()
        ejecucion.bloqueos = comprobar(sentencias, tablas)
        ejecucion.bloqueos += [Bloqueo(h.linea or 0, h.mensaje)
                               for h in criticos]
        if ejecucion.bloqueos and not forzar:
            ejecucion.motivo = ("Hay sentencias que no se pueden ejecutar "
                                "con respaldo (usa --forzar para "
                                "ejecutarlas sin él).")
            return ejecucion

        try:
            preparar_transaccion(conexion, statement_timeout, lock_timeout)
            # Si el cliente desaparece esperando la confirmación, el
            # servidor deshace la transacción solo.
            conexion.execute(
                "SELECT set_config('idle_in_transaction_session_timeout', "
                "%s, true)", (f"{int(tiempo_confirmacion) + 30}s",))
            conexion.execute(SQL_CREAR)
            conexion.execute(
                f"INSERT INTO {ESQUEMA}.ejecuciones (id, script, hash, sql) "
                f"VALUES (%s, %s, %s, %s)",
                (ejecucion.id, nombre_script,
                 hashlib.sha256(fuente.encode("utf-8")).hexdigest(),
                 fuente))
            _ejecutar_sentencias(conexion, ejecucion, sentencias, tablas,
                                 temporales, ignorar, forzar)

            if ejecucion.sin_respaldo and not permitir_indirectos:
                ejecucion.motivo = (
                    "Hay cambios que el respaldo no cubre (triggers, "
                    "cascadas o sentencias forzadas). Revisa la lista y "
                    "usa --permitir-indirectos o --ignorar-tabla.")
                return ejecucion
            conexion.execute(
                f"UPDATE {ESQUEMA}.ejecuciones SET sin_respaldo = %s "
                f"WHERE id = %s",
                (json.dumps(_resumen_sin_respaldo(ejecucion)),
                 ejecucion.id))
            if confirmar(ejecucion):
                conexion.commit()
                ejecucion.aplicada = True
            else:
                ejecucion.motivo = "No se confirmó: se ha hecho ROLLBACK."
        finally:
            if not ejecucion.aplicada:
                conexion.rollback()
    return ejecucion


def _ejecutar_sentencias(conexion, ejecucion, sentencias, tablas,
                         temporales, ignorar, forzar):
    with conexion.cursor() as cur:
        antes = estadisticas(conexion)
        for s in sentencias:
            if s.tipo == TRANSACCION:
                continue
            tabla = (tablas.get(s.tablas_escritas[0])
                     if s.tablas_escritas else None)
            respaldable = (s.tipo == MODIFICACION
                           and not _solo_temporales(s, temporales)
                           and _motivo_sin_respaldo(s, tabla) is None)
            filas = respaldadas = None
            try:
                if respaldable:
                    filas, respaldadas = _ejecutar_con_respaldo(
                        conexion, cur, ejecucion.id, s, tabla)
                elif (isinstance(s.nodo, ast.DropStmt)
                      and _solo_temporales(s, temporales)):
                    cur.execute(_texto_drop_temporal(s))
                else:
                    cur.execute(s.texto)
                    filas = cur.rowcount if cur.rowcount >= 0 else None
            except psycopg.Error as error:
                raise ErrorEjecucion(s.linea, mensaje_error(error))

            despues = estadisticas(conexion)
            efectos = diferencia(antes, despues)
            antes = despues
            if s.tipo == CONFIGURACION or (
                    isinstance(s.nodo, ast.DropStmt)
                    and _solo_temporales(s, temporales)):
                continue
            paso = Paso(s.indice, s.linea, s.operacion,
                        s.tablas_escritas[0] if s.tablas_escritas else None,
                        filas, respaldadas)
            if s.tipo == CONSULTA:
                paso.nota = "Filas devueltas por la consulta."
            elif not respaldable and not _solo_temporales(s, temporales):
                paso.forzada = True
                paso.nota = "Ejecutada SIN respaldo (--forzar)."
            _repartir_efectos(paso, efectos, ignorar)
            ejecucion.pasos.append(paso)
