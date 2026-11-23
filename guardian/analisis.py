"""Análisis estático del script: reglas que no necesitan base de datos."""

import re
from dataclasses import dataclass

from pglast import ast

from guardian.sentencias import (
    CONFIGURACION, DDL, MODIFICACION, TRANSACCION, modifica_dentro,
    recorrer, tablas_temporales,
)

# Niveles de gravedad, de mayor a menor.
CRITICO = "crítico"
ALTO = "alto"
MEDIO = "medio"
INFO = "info"
NIVELES = (CRITICO, ALTO, MEDIO, INFO)

_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}")
_COLUMNAS_USUARIO = {"updated_by", "deleted_by", "created_by"}
_COLUMNAS_BORRADO_LOGICO = {"estado", "deleted_at", "deleted_by", "activo",
                            "is_deleted", "deleted"}
# Subcomandos de ALTER TABLE que no bloquean la tabla entera.
_ALTER_LIGEROS = {"AT_SetStatistics", "AT_SetOptions", "AT_ResetOptions",
                  "AT_SetStorage", "AT_SetCompression",
                  "AT_ValidateConstraint"}


@dataclass
class Hallazgo:
    nivel: str
    codigo: str
    mensaje: str
    linea: int | None = None
    sentencia: int | None = None


def _hallazgo(nivel, codigo, mensaje, sentencia=None):
    return Hallazgo(
        nivel=nivel,
        codigo=codigo,
        mensaje=mensaje,
        linea=sentencia.linea if sentencia else None,
        sentencia=sentencia.indice if sentencia else None,
    )


def _es_temporal(tabla, temporales):
    return tabla in temporales or tabla.startswith("pg_temp.")


def _escribe_permanente(s, temporales):
    """True si la sentencia escribe en alguna tabla que no es temporal."""
    if s.tipo not in (MODIFICACION, DDL):
        return False
    if not s.tablas_escritas:
        return True
    return any(not _es_temporal(t, temporales) for t in s.tablas_escritas)


def _reglas_transaccion(sentencias, temporales):
    hallazgos = []
    if not any(_escribe_permanente(s, temporales) for s in sentencias):
        return hallazgos

    primera = next((s for s in sentencias if s.tipo != CONFIGURACION), None)
    if primera is None or primera.operacion not in ("BEGIN", "START"):
        hallazgos.append(_hallazgo(
            ALTO, "TX001",
            "El script no empieza con BEGIN: cada sentencia se confirma "
            "por separado y no se puede deshacer si algo falla a mitad.",
            primera))

    cierres = [s for s in sentencias if s.tipo == TRANSACCION
               and s.operacion in ("COMMIT", "ROLLBACK")]
    ultimo = cierres[-1] if cierres else None
    for s in cierres[:-1]:
        if s.operacion == "COMMIT":
            hallazgos.append(_hallazgo(
                ALTO, "TX004",
                "COMMIT a mitad del script: lo anterior queda aplicado "
                "aunque falle lo que viene después.", s))
    if ultimo is not None and ultimo.operacion == "COMMIT":
        hallazgos.append(_hallazgo(
            ALTO, "TX002",
            "El script termina con COMMIT y aplica los cambios en cuanto se "
            "ejecuta. Lo seguro es terminar con ROLLBACK, revisar el "
            "resultado y cambiarlo a COMMIT después.", ultimo))

    posteriores = [s for s in sentencias
                   if (ultimo is None or s.indice > ultimo.indice)
                   and _escribe_permanente(s, temporales)]
    if posteriores and primera is not None and primera.operacion in (
            "BEGIN", "START"):
        hallazgos.append(_hallazgo(
            MEDIO, "TX003",
            "Hay cambios después del último COMMIT/ROLLBACK (o no hay "
            "ninguno): el resultado depende de la herramienta con la que "
            "se ejecute.", posteriores[0]))

    comienzos = [s for s in sentencias if s.tipo == TRANSACCION
                 and s.operacion in ("BEGIN", "START")]
    if len(comienzos) > 1:
        hallazgos.append(_hallazgo(
            MEDIO, "TX005",
            f"El script tiene {len(comienzos)} transacciones. Si una falla, "
            "las anteriores ya estarán confirmadas.", comienzos[1]))
    return hallazgos


def _valor_constante(nodo):
    if not isinstance(nodo, ast.A_Const) or nodo.isnull:
        return None
    valor = nodo.val
    return getattr(valor, "ival", getattr(valor, "sval", None))


def _reglas_resumen(sentencias, temporales):
    """Avisos informativos que se agrupan en uno para todo el script."""
    hallazgos = []
    borrado_logico = {}
    usuarios = {}
    for s in sentencias:
        if (not isinstance(s.nodo, ast.UpdateStmt)
                or not _escribe_permanente(s, temporales)):
            continue
        if _COLUMNAS_BORRADO_LOGICO & set(s.columnas):
            borrado_logico.setdefault(s.tablas_escritas[0], s)
        for objetivo in s.nodo.targetList or ():
            valor = _valor_constante(objetivo.val)
            if objetivo.name in _COLUMNAS_USUARIO and valor is not None:
                usuarios.setdefault(valor, s)
    if borrado_logico:
        hallazgos.append(_hallazgo(
            INFO, "DML004",
            "Borrado lógico (estado, deleted_at...) en: "
            f"{', '.join(borrado_logico)}.",
            next(iter(borrado_logico.values()))))
    if usuarios:
        lista = ", ".join(str(u) for u in usuarios)
        hallazgos.append(_hallazgo(
            INFO, "DML005",
            f"Usuario fijo en updated_by/deleted_by ({lista}): comprueba "
            "que es el correcto.", next(iter(usuarios.values()))))
    return hallazgos


def _reglas_modificacion(s, temporales):
    hallazgos = []
    nodo = s.nodo
    tabla = s.tablas_escritas[0] if s.tablas_escritas else ""
    if _es_temporal(tabla, temporales):
        return hallazgos

    if isinstance(nodo, (ast.UpdateStmt, ast.DeleteStmt)):
        if nodo.whereClause is None:
            hallazgos.append(_hallazgo(
                CRITICO, "DML001",
                f"{s.operacion} sobre {tabla} sin WHERE: afecta a todas "
                "las filas de la tabla.", s))
    if isinstance(nodo, ast.DeleteStmt):
        hallazgos.append(_hallazgo(
            MEDIO, "DML002",
            f"Borrado físico en {tabla}. Si la tabla usa borrado lógico "
            "(estado, deleted_at...), es más seguro marcar las filas.", s))
    if isinstance(nodo, ast.TruncateStmt):
        tablas = [t for t in s.tablas_escritas
                  if not _es_temporal(t, temporales)]
        if tablas:
            hallazgos.append(_hallazgo(
                CRITICO, "DML003",
                f"TRUNCATE de {', '.join(tablas)}: vacía la tabla entera y "
                "bloquea cualquier acceso mientras dura.", s))
    return hallazgos


def _reglas_ddl(s, temporales):
    hallazgos = []
    nodo = s.nodo
    if isinstance(nodo, ast.DropStmt):
        permanentes = [t for t in s.tablas_escritas
                       if not _es_temporal(t, temporales)]
        temporales_sin_esquema = [t for t in s.tablas_escritas
                                  if t in temporales]
        if s.operacion != "DROP TABLE" or permanentes:
            objetos = ", ".join(permanentes) or s.operacion
            hallazgos.append(_hallazgo(
                CRITICO, "DDL001",
                f"{s.operacion} de {objetos}: se elimina el objeto y sus "
                "datos.", s))
        for tabla in temporales_sin_esquema:
            hallazgos.append(_hallazgo(
                MEDIO, "DDL002",
                f"DROP TABLE {tabla} sin esquema: si la temporal aún no "
                "existe y hay una tabla permanente con ese nombre, se "
                "borraría la permanente. Usa pg_temp." + tabla + ".", s))
    elif isinstance(nodo, ast.AlterTableStmt):
        if s.tablas_escritas[0] not in temporales:
            subcomandos = {getattr(c, "subtype", None) for c in nodo.cmds}
            nombres = {getattr(st, "name", str(st)) for st in subcomandos}
            if not nombres <= _ALTER_LIGEROS:
                hallazgos.append(_hallazgo(
                    ALTO, "DDL003",
                    f"ALTER TABLE {s.tablas_escritas[0]} bloquea la tabla "
                    "(ACCESS EXCLUSIVE) mientras dura: nadie puede leerla "
                    "ni escribirla. Algunos cambios reescriben la tabla "
                    "entera.", s))
    elif isinstance(nodo, ast.IndexStmt):
        if not nodo.concurrent and s.tablas_escritas[0] not in temporales:
            hallazgos.append(_hallazgo(
                MEDIO, "DDL004",
                f"CREATE INDEX sin CONCURRENTLY bloquea las escrituras en "
                f"{s.tablas_escritas[0]} hasta que termina.", s))
    elif s.operacion in ("CREATE TABLE", "CREATE TABLE AS"):
        hallazgos.append(_hallazgo(
            INFO, "DDL005",
            f"Se crea la tabla permanente {s.tablas_escritas[0]}.", s))
    return hallazgos


def _reglas_generales(s):
    hallazgos = []
    fechas = sorted({n.val.sval for n in recorrer(s.nodo)
                     if isinstance(n, ast.A_Const)
                     and isinstance(n.val, ast.String)
                     and _FECHA.match(n.val.sval)})
    if fechas:
        hallazgos.append(_hallazgo(
            INFO, "GEN001",
            f"Fechas fijas en la sentencia ({', '.join(fechas)}): "
            "comprueba que siguen siendo las correctas.", s))
    if s.tipo != MODIFICACION and modifica_dentro(s.nodo):
        hallazgos.append(_hallazgo(
            ALTO, "GEN002",
            "La sentencia modifica datos dentro de un WITH, aunque parece "
            "una consulta.", s))
    if s.tipo == CONFIGURACION and not getattr(s.nodo, "is_local", True):
        hallazgos.append(_hallazgo(
            INFO, "GEN003",
            "SET sin LOCAL: el valor sigue activo en la sesión después de "
            "la transacción.", s))
    return hallazgos


def analizar(sentencias):
    """Aplica todas las reglas y devuelve los hallazgos ordenados."""
    temporales = tablas_temporales(sentencias)
    hallazgos = _reglas_transaccion(sentencias, temporales)
    hallazgos += _reglas_resumen(sentencias, temporales)
    for s in sentencias:
        if s.tipo == MODIFICACION:
            hallazgos += _reglas_modificacion(s, temporales)
        elif s.tipo == DDL:
            hallazgos += _reglas_ddl(s, temporales)
        hallazgos += _reglas_generales(s)
    hallazgos.sort(key=lambda h: (NIVELES.index(h.nivel), h.linea or 0))
    return hallazgos
