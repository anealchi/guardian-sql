"""Cálculo del impacto de un script contra una base de datos real.

Hay dos modos y los dos terminan siempre con ROLLBACK:

- **conteo** (por defecto): no escribe en ninguna tabla permanente. Crea las
  tablas temporales del script y convierte cada UPDATE/DELETE/INSERT en un
  `SELECT count(*)` con las mismas condiciones.
- **simulación**: ejecuta el script completo dentro de una transacción y
  mide cuántas filas cambia cada sentencia en cada tabla, incluidas las que
  cambian los triggers. Bloquea filas mientras dura, así que está pensado
  para una copia o réplica de staging (o producción con `lock_timeout`).
"""

from dataclasses import dataclass, field

import psycopg
from pglast import ast

from guardian.analisis import ALTO, CRITICO, MEDIO, Hallazgo
from guardian.esquema import leer_tablas
from guardian.sentencias import (
    CONFIGURACION, CONSULTA, DDL, MODIFICACION, TRANSACCION, deparsear,
    modifica_dentro, tablas_leidas, tablas_permanentes_escritas,
    tablas_temporales,
)

EXACTO = "exacto"
APROXIMADO = "aproximado"
NO_SIMULADO = "no simulado"
ERROR = "error"

_SQL_ESTADISTICAS = """
select schemaname || '.' || relname, n_tup_ins, n_tup_upd, n_tup_del
from pg_stat_xact_user_tables
where schemaname not like 'pg\\_temp%%'
    and (n_tup_ins + n_tup_upd + n_tup_del) > 0
"""

# Sentencias que PostgreSQL no permite dentro de una transacción.
FUERA_DE_TRANSACCION = (ast.VacuumStmt, ast.CreatedbStmt, ast.DropdbStmt,
                        ast.CreateTableSpaceStmt, ast.DropTableSpaceStmt,
                        ast.AlterSystemStmt, ast.ReindexStmt)


@dataclass
class ResultadoSentencia:
    indice: int
    linea: int
    operacion: str
    tabla: str | None
    filas: int | None
    precision: str
    nota: str = ""
    # Simulación: {tabla: (insertadas, actualizadas, borradas)}.
    efectos: dict = field(default_factory=dict)


@dataclass
class Impacto:
    modo: str
    resultados: list
    hallazgos: list
    tablas: dict
    servidor: str = ""


def alias_de(relacion):
    """Nombre con el que la sentencia se refiere a su tabla objetivo."""
    return relacion.alias.aliasname if relacion.alias else relacion.relname


def prefijo_with(nodo):
    """Cláusula WITH de la sentencia, lista para anteponerla a otro SQL."""
    if nodo.withClause is None:
        return ""
    recursivo = "RECURSIVE " if nodo.withClause.recursive else ""
    return f"WITH {recursivo}{deparsear(nodo.withClause)} "


def origen_de_modificacion(nodo):
    """Partes de un UPDATE/DELETE para reutilizarlas en un SELECT.

    Devuelve (origen, donde, alias, con_cruces): el FROM equivalente (tabla
    objetivo más FROM/USING), el WHERE (o ""), el alias de la tabla
    objetivo y si hay otras tablas cruzadas.
    """
    extra = (nodo.fromClause if isinstance(nodo, ast.UpdateStmt)
             else nodo.usingClause) or ()
    origen = ", ".join([deparsear(nodo.relation)]
                       + [deparsear(f) for f in extra])
    donde = (f" WHERE {deparsear(nodo.whereClause)}"
             if nodo.whereClause is not None else "")
    return origen, donde, alias_de(nodo.relation), bool(extra)


def consulta_de_conteo(nodo):
    """SQL que cuenta las filas que tocaría un UPDATE/DELETE/INSERT.

    Devuelve None si la sentencia no se puede contar sin ejecutarla.
    """
    if isinstance(nodo, (ast.UpdateStmt, ast.DeleteStmt)):
        origen, donde, alias, cruces = origen_de_modificacion(nodo)
        # Con FROM/USING una fila puede cruzarse varias veces, pero
        # PostgreSQL solo la modifica una: se cuentan filas distintas.
        contador = (f"count(DISTINCT ({alias}.tableoid, {alias}.ctid))"
                    if cruces else "count(*)")
        return f"{prefijo_with(nodo)}SELECT {contador} FROM {origen}{donde}"
    if isinstance(nodo, ast.InsertStmt):
        select = nodo.selectStmt
        if select is None:
            return "SELECT 1"
        if isinstance(select, ast.SelectStmt) and select.valuesLists:
            return f"SELECT {len(select.valuesLists)}"
        return (f"{prefijo_with(nodo)}SELECT count(*) FROM "
                f"({deparsear(select)}) AS guardian_origen")
    if isinstance(nodo, ast.TruncateStmt):
        partes = [f"(SELECT count(*) FROM {deparsear(r)})"
                  for r in nodo.relations]
        return "SELECT " + " + ".join(partes)
    return None


def hallazgos_de_esquema(sentencias, tablas):
    """Riesgos que se ven al cruzar el script con el esquema real."""
    hallazgos = []
    temporales = tablas_temporales(sentencias)
    for s in sentencias:
        if s.tipo not in (MODIFICACION, DDL):
            continue
        for nombre in s.tablas_escritas:
            if nombre in temporales or nombre not in tablas:
                continue
            tabla = tablas[nombre]
            if not tabla.existe:
                if s.operacion.startswith(("CREATE", "DROP")):
                    continue
                hallazgos.append(Hallazgo(
                    ALTO, "ESQ001",
                    f"La tabla {nombre} no existe en esta base de datos.",
                    s.linea, s.indice))
                continue
            operacion = s.operacion
            disparados = [t for t in tabla.triggers
                          if t.se_dispara(operacion, s.columnas)]
            if disparados:
                lista = "; ".join(t.describir() for t in disparados)
                hallazgos.append(Hallazgo(
                    MEDIO, "ESQ002",
                    f"{operacion} en {nombre} dispara "
                    f"{len(disparados)} trigger(s): {lista}. Pueden "
                    "modificar otras tablas.", s.linea, s.indice))
            if operacion in ("DELETE", "TRUNCATE"):
                cascada = [(c, o) for c, o, accion in
                           tabla.referencias_entrantes
                           if accion in ("CASCADE", "SET NULL",
                                         "SET DEFAULT")]
                if cascada:
                    origenes = ", ".join(sorted({o for _, o in cascada}))
                    hallazgos.append(Hallazgo(
                        CRITICO if operacion == "TRUNCATE" else ALTO,
                        "ESQ003",
                        f"Borrar en {nombre} afecta en cascada a: "
                        f"{origenes}.", s.linea, s.indice))
            if tabla.es_vista:
                hallazgos.append(Hallazgo(
                    MEDIO, "ESQ004",
                    f"{nombre} es una vista: la escritura se traslada a sus "
                    "tablas base.", s.linea, s.indice))
    return hallazgos


TIEMPO_CONEXION = 10


def conectar(dsn, **opciones):
    """Abre una conexión con un tiempo máximo de espera al conectar.

    Sin él, un servidor caído puede tener la petición colgada minutos. Si
    la cadena ya trae `connect_timeout`, se respeta.
    """
    from psycopg.conninfo import conninfo_to_dict

    if "connect_timeout" not in conninfo_to_dict(dsn):
        opciones.setdefault("connect_timeout", TIEMPO_CONEXION)
    return psycopg.connect(dsn, **opciones)


def preparar_transaccion(conexion, statement_timeout, lock_timeout):
    with conexion.cursor() as cur:
        cur.execute("SELECT set_config('statement_timeout', %s, true), "
                    "set_config('lock_timeout', %s, true), "
                    "set_config('application_name', 'sql-guardian', true)",
                    (statement_timeout, lock_timeout))


def _ejecutar_con_savepoint(cur, sql):
    """Ejecuta `sql` en `cur`; si falla, deshace solo esa sentencia.

    Los SAVEPOINT van por otro cursor para no perder el resultado (filas y
    rowcount) de `cur`.
    """
    conexion = cur.connection
    conexion.execute("SAVEPOINT guardian")
    try:
        cur.execute(sql)
    except psycopg.Error:
        conexion.execute("ROLLBACK TO SAVEPOINT guardian")
        raise
    conexion.execute("RELEASE SAVEPOINT guardian")


def mensaje_error(error):
    diag = getattr(error, "diag", None)
    texto = diag.message_primary if diag and diag.message_primary else ""
    return texto or str(error).strip().splitlines()[0]


def contar(conexion, sentencias, statement_timeout="60s",
           lock_timeout="3s"):
    """Modo conteo: estima las filas afectadas sin escribir nada."""
    temporales = tablas_temporales(sentencias)
    escritas_antes = set()
    resultados = []
    try:
        preparar_transaccion(conexion, statement_timeout, lock_timeout)
        with conexion.cursor() as cur:
            for s in sentencias:
                resultado = _contar_sentencia(cur, s, temporales,
                                              escritas_antes)
                if resultado is not None:
                    resultados.append(resultado)
                if s.tipo == MODIFICACION:
                    escritas_antes.update(t for t in s.tablas_escritas
                                          if t not in temporales)
    finally:
        conexion.rollback()
    return resultados


def _contar_sentencia(cur, s, temporales, escritas_antes):
    def resultado(filas, precision, nota="", tabla=None):
        if tabla is None and s.tablas_escritas:
            tabla = s.tablas_escritas[0]
        return ResultadoSentencia(s.indice, s.linea, s.operacion, tabla,
                                  filas, precision, nota)

    if s.tipo in (TRANSACCION, CONSULTA):
        return None
    if modifica_dentro(s.nodo):
        return resultado(None, NO_SIMULADO,
                         "Modifica datos dentro de un WITH: no se ejecuta.")
    if s.tipo == CONFIGURACION:
        try:
            _ejecutar_con_savepoint(cur, s.texto)
        except psycopg.Error as error:
            return resultado(None, ERROR, mensaje_error(error))
        return None

    # Solo se ejecuta lo que escribe exclusivamente en tablas temporales del
    # script. Sin tablas conocidas (DO, CALL, COPY...) nunca se ejecuta.
    temporal = (s.tipo in (MODIFICACION, DDL) and bool(s.tablas_escritas)
                and all(t in temporales for t in s.tablas_escritas))
    if s.operacion.startswith("DROP"):
        if temporal:
            return None
        return resultado(None, NO_SIMULADO,
                         "No se ejecuta en el modo conteo.")
    if temporal and s.operacion not in ("ALTER TABLE", "CREATE INDEX"):
        # Tablas temporales del propio script: se crean de verdad porque
        # las sentencias siguientes las usan.
        try:
            _ejecutar_con_savepoint(cur, s.texto)
        except psycopg.Error as error:
            return resultado(None, ERROR, mensaje_error(error))
        if s.operacion in ("CREATE TEMP TABLE AS", "SELECT INTO"):
            return resultado(cur.rowcount, EXACTO, "Tabla temporal.")
        return None

    sql = consulta_de_conteo(s.nodo) if s.tipo == MODIFICACION else None
    if sql is None:
        return resultado(None, NO_SIMULADO,
                         "No se ejecuta en el modo conteo.")
    try:
        _ejecutar_con_savepoint(cur, sql)
        filas = cur.fetchone()[0]
    except psycopg.Error as error:
        return resultado(None, ERROR, mensaje_error(error))

    dependencias = (tablas_leidas(s.nodo) | set(s.tablas_escritas)) \
        & escritas_antes
    if dependencias:
        return resultado(
            filas, APROXIMADO,
            "Depende de cambios anteriores en "
            f"{', '.join(sorted(dependencias))}; el modo simulación da el "
            "número exacto.")
    if s.operacion == "INSERT" and s.nodo.onConflictClause is not None:
        return resultado(filas, APROXIMADO,
                         "ON CONFLICT puede evitar algunas inserciones.")
    return resultado(filas, EXACTO)


def estadisticas(conexion):
    filas = conexion.execute(_SQL_ESTADISTICAS).fetchall()
    return {tabla: (ins, upd, dele) for tabla, ins, upd, dele in filas}


def diferencia(antes, despues):
    efectos = {}
    for tabla, valores in despues.items():
        previos = antes.get(tabla, (0, 0, 0))
        delta = tuple(a - b for a, b in zip(valores, previos))
        if any(delta):
            efectos[tabla] = delta
    return efectos


def simular(conexion, sentencias, statement_timeout="60s",
            lock_timeout="3s"):
    """Modo simulación: ejecuta el script y lo deshace con ROLLBACK."""
    for s in sentencias:
        if isinstance(s.nodo, FUERA_DE_TRANSACCION) or (
                isinstance(s.nodo, ast.IndexStmt) and s.nodo.concurrent):
            raise ValueError(
                f"La sentencia de la línea {s.linea} ({s.operacion}) no se "
                "puede ejecutar dentro de una transacción, así que el "
                "script no se puede simular.")
    temporales = tablas_temporales(sentencias)
    resultados = []
    try:
        preparar_transaccion(conexion, statement_timeout, lock_timeout)
        with conexion.cursor() as cur:
            antes = estadisticas(conexion)
            for s in sentencias:
                if s.tipo == TRANSACCION:
                    continue
                try:
                    cur.execute(s.texto)
                    filas = cur.rowcount if cur.rowcount >= 0 else None
                except psycopg.Error as error:
                    resultados.append(ResultadoSentencia(
                        s.indice, s.linea, s.operacion,
                        s.tablas_escritas[0] if s.tablas_escritas else None,
                        None, ERROR, mensaje_error(error)))
                    break
                despues = estadisticas(conexion)
                efectos = diferencia(antes, despues)
                antes = despues
                if s.tipo == CONFIGURACION or (
                        s.operacion.startswith("DROP")
                        and set(s.tablas_escritas) <= temporales):
                    continue
                nota = ""
                if s.tipo == CONSULTA:
                    nota = "Filas devueltas por la consulta."
                resultados.append(ResultadoSentencia(
                    s.indice, s.linea, s.operacion,
                    s.tablas_escritas[0] if s.tablas_escritas else None,
                    filas, EXACTO, nota, efectos))
    finally:
        conexion.rollback()
    return resultados


def _sin_esquema(tabla):
    return tabla.split(".", 1)[-1]


def hallazgos_de_simulacion(resultados):
    """Cambios en tablas distintas de la que nombra cada sentencia."""
    hallazgos = []
    for r in resultados:
        propia = _sin_esquema(r.tabla) if r.tabla else None
        otras = sorted(t for t in r.efectos if _sin_esquema(t) != propia)
        if otras:
            hallazgos.append(Hallazgo(
                MEDIO, "SIM001",
                f"{r.operacion} {r.tabla or ''} cambia también "
                f"{', '.join(otras)} (triggers o cascadas).".replace(
                    "  ", " "), r.linea, r.indice))
    return hallazgos


def calcular(dsn, sentencias, modo="conteo", **tiempos):
    """Conecta, lee el esquema, calcula el impacto y hace ROLLBACK."""
    with conectar(dsn, autocommit=False) as conexion:
        servidor = conexion.info.parameter_status("server_version") or ""
        nombres = tablas_permanentes_escritas(sentencias)
        tablas = leer_tablas(conexion, nombres)
        conexion.rollback()
        hallazgos = hallazgos_de_esquema(sentencias, tablas)
        if modo == "simulacion":
            resultados = simular(conexion, sentencias, **tiempos)
            hallazgos += hallazgos_de_simulacion(resultados)
        else:
            resultados = contar(conexion, sentencias, **tiempos)
    return Impacto(modo=modo, resultados=resultados, hallazgos=hallazgos,
                   tablas=tablas, servidor=servidor)
