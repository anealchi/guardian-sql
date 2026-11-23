"""Lectura de un script SQL y clasificación de cada sentencia.

Usa pglast (el parser real de PostgreSQL) para dividir el script en
sentencias y saber, para cada una, qué tipo es y qué tablas modifica.
"""

import re
from dataclasses import dataclass, field

from pglast import ast, parse_sql
from pglast.enums import ObjectType, TransactionStmtKind
from pglast.parser import ParseError
from pglast.stream import RawStream

# Comentarios y espacios que pglast incluye al principio de cada sentencia.
_INICIO_IGNORABLE = re.compile(r"(\s+|--[^\n]*|/\*.*?\*/)*", re.DOTALL)

# Tipos de sentencia que usa el resto del programa.
TRANSACCION = "transaccion"
CONSULTA = "consulta"
MODIFICACION = "modificacion"
DDL = "ddl"
CONFIGURACION = "configuracion"
OTRA = "otra"


class ErrorSintaxis(Exception):
    """El script no es SQL válido para PostgreSQL."""

    def __init__(self, mensaje, linea):
        super().__init__(f"Línea {linea}: {mensaje}")
        self.linea = linea


@dataclass
class Sentencia:
    """Una sentencia del script, con lo necesario para analizarla."""

    indice: int
    linea: int
    texto: str
    nodo: ast.Node
    tipo: str
    # Nombre de la operación: UPDATE, DELETE, CREATE TEMP TABLE...
    operacion: str
    # Tablas que la sentencia escribe (tal y como aparecen en el script).
    tablas_escritas: list = field(default_factory=list)
    # Columnas que asigna un UPDATE.
    columnas: list = field(default_factory=list)

    @property
    def resumen(self):
        """Primera línea del texto, para mostrarla en el informe."""
        primera = self.texto.splitlines()[0] if self.texto else ""
        return primera if len(primera) <= 70 else primera[:67] + "..."


def nombre_tabla(rangevar):
    """Nombre de una tabla (con su esquema, si lo tiene) de un RangeVar."""
    if rangevar.schemaname:
        return f"{rangevar.schemaname}.{rangevar.relname}"
    return rangevar.relname


def deparsear(nodo):
    """Convierte un nodo del árbol otra vez en SQL."""
    return RawStream()(nodo)


def recorrer(nodo):
    """Recorre en profundidad todos los nodos que cuelgan de `nodo`."""
    if isinstance(nodo, (list, tuple)):
        for hijo in nodo:
            yield from recorrer(hijo)
    elif isinstance(nodo, ast.Node):
        yield nodo
        for atributo in type(nodo).__slots__:
            yield from recorrer(getattr(nodo, atributo))


def _linea_de(fuente, posicion):
    return fuente.count("\n", 0, posicion) + 1


def _tablas_de_drop(nodo):
    tablas = []
    for objeto in nodo.objects or ():
        partes = [p.sval for p in objeto if isinstance(p, ast.String)]
        if partes:
            tablas.append(".".join(partes))
    return tablas


def _clasificar(nodo):
    """Devuelve (tipo, operación, tablas escritas, columnas)."""
    if isinstance(nodo, ast.TransactionStmt):
        operacion = TransactionStmtKind(nodo.kind).name.replace(
            "TRANS_STMT_", "")
        return TRANSACCION, operacion, [], []
    if isinstance(nodo, ast.SelectStmt):
        if nodo.intoClause is not None:
            return DDL, "SELECT INTO", [nombre_tabla(nodo.intoClause.rel)], []
        return CONSULTA, "SELECT", [], []
    if isinstance(nodo, ast.UpdateStmt):
        columnas = [t.name for t in nodo.targetList or ()]
        return (MODIFICACION, "UPDATE", [nombre_tabla(nodo.relation)],
                columnas)
    if isinstance(nodo, ast.DeleteStmt):
        return MODIFICACION, "DELETE", [nombre_tabla(nodo.relation)], []
    if isinstance(nodo, ast.InsertStmt):
        return MODIFICACION, "INSERT", [nombre_tabla(nodo.relation)], []
    if isinstance(nodo, ast.MergeStmt):
        return MODIFICACION, "MERGE", [nombre_tabla(nodo.relation)], []
    if isinstance(nodo, ast.TruncateStmt):
        tablas = [nombre_tabla(r) for r in nodo.relations]
        return MODIFICACION, "TRUNCATE", tablas, []
    if isinstance(nodo, ast.CreateTableAsStmt):
        rel = nodo.into.rel
        temporal = rel.relpersistence == "t"
        operacion = "CREATE TEMP TABLE AS" if temporal else "CREATE TABLE AS"
        return DDL, operacion, [nombre_tabla(rel)], []
    if isinstance(nodo, ast.CreateStmt):
        temporal = nodo.relation.relpersistence == "t"
        operacion = "CREATE TEMP TABLE" if temporal else "CREATE TABLE"
        return DDL, operacion, [nombre_tabla(nodo.relation)], []
    if isinstance(nodo, ast.DropStmt):
        tipo_objeto = ObjectType(nodo.removeType).name.replace("OBJECT_", "")
        tablas = _tablas_de_drop(nodo) if tipo_objeto == "TABLE" else []
        return DDL, f"DROP {tipo_objeto}", tablas, []
    if isinstance(nodo, ast.AlterTableStmt):
        return DDL, "ALTER TABLE", [nombre_tabla(nodo.relation)], []
    if isinstance(nodo, ast.IndexStmt):
        return DDL, "CREATE INDEX", [nombre_tabla(nodo.relation)], []
    if isinstance(nodo, (ast.VariableSetStmt, ast.VariableShowStmt)):
        return CONFIGURACION, "SET", [], []
    nombre = type(nodo).__name__.removesuffix("Stmt").upper()
    return OTRA, nombre, [], []


def leer_script(fuente):
    """Divide el script en una lista de `Sentencia`.

    Lanza `ErrorSintaxis` si PostgreSQL no podría ejecutarlo.
    """
    fuente = fuente.lstrip("﻿")
    try:
        crudas = parse_sql(fuente)
    except ParseError as error:
        # pglast da el mensaje y la posición (en caracteres) del error.
        posicion = error.args[1] if len(error.args) > 1 else 0
        mensaje = str(error.args[0]).split(", at index")[0]
        raise ErrorSintaxis(mensaje, _linea_de(fuente, posicion or 0))

    sentencias = []
    for indice, cruda in enumerate(crudas, start=1):
        inicio = cruda.stmt_location
        fin = inicio + cruda.stmt_len if cruda.stmt_len else len(fuente)
        salto = _INICIO_IGNORABLE.match(fuente, inicio).end()
        inicio = min(salto, fin)
        texto = fuente[inicio:fin].strip()
        tipo, operacion, tablas, columnas = _clasificar(cruda.stmt)
        sentencias.append(Sentencia(
            indice=indice,
            linea=_linea_de(fuente, inicio),
            texto=texto,
            nodo=cruda.stmt,
            tipo=tipo,
            operacion=operacion,
            tablas_escritas=tablas,
            columnas=columnas,
        ))
    return sentencias


def tablas_temporales(sentencias):
    """Nombres de las tablas temporales que crea el propio script."""
    nombres = set()
    for s in sentencias:
        if s.operacion in ("CREATE TEMP TABLE AS", "CREATE TEMP TABLE"):
            nombres.update(s.tablas_escritas)
        elif (s.operacion == "SELECT INTO"
              and s.nodo.intoClause.rel.relpersistence == "t"):
            nombres.update(s.tablas_escritas)
    return nombres


def tablas_permanentes_escritas(sentencias):
    """Tablas no temporales que el script modifica, en orden de aparición."""
    temporales = tablas_temporales(sentencias)
    vistas = []
    for s in sentencias:
        if s.tipo not in (MODIFICACION, DDL):
            continue
        for tabla in s.tablas_escritas:
            if tabla not in temporales and tabla not in vistas:
                vistas.append(tabla)
    return vistas


def modifica_dentro(nodo):
    """True si una sentencia esconde un INSERT/UPDATE/DELETE (en un WITH)."""
    return any(isinstance(n, (ast.InsertStmt, ast.UpdateStmt,
                              ast.DeleteStmt, ast.MergeStmt))
               for n in recorrer(nodo) if n is not nodo)


def tablas_leidas(nodo):
    """Nombres de todas las tablas que aparecen en una sentencia."""
    return {nombre_tabla(n) for n in recorrer(nodo)
            if isinstance(n, ast.RangeVar)}
