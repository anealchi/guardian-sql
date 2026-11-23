"""Lectura del esquema de la base de datos desde pg_catalog."""

from dataclasses import dataclass, field

# Bits de pg_trigger.tgtype.
_TG_ROW = 1
_TG_BEFORE = 2
_TG_INSERT = 4
_TG_DELETE = 8
_TG_UPDATE = 16
_TG_TRUNCATE = 32
_TG_INSTEAD = 64

_SQL_TABLA = """
select c.oid, format('%%I.%%I', n.nspname, c.relname),
    greatest(c.reltuples, 0)::bigint, c.relkind
from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
where c.oid = to_regclass(%s)
"""

_SQL_CLAVE = """
select a.attname
from pg_index i
    join pg_attribute a on a.attrelid = i.indrelid
        and a.attnum = any (i.indkey)
where i.indrelid = %s and i.indisprimary
order by array_position(i.indkey, a.attnum)
"""

_SQL_COLUMNAS = """
select attname, attgenerated <> ''
from pg_attribute
where attrelid = %s and attnum > 0 and not attisdropped
order by attnum
"""

_SQL_TRIGGERS = """
select t.tgname, t.tgtype, t.tgenabled, p.proname,
    coalesce((
        select array_agg(a.attname order by a.attnum)
        from pg_attribute a
        where a.attrelid = t.tgrelid and a.attnum = any (t.tgattr)
    ), '{}') as columnas
from pg_trigger t
    join pg_proc p on p.oid = t.tgfoid
where t.tgrelid = %s and not t.tgisinternal
order by t.tgname
"""

_SQL_FK_ENTRANTES = """
select con.conname, src.relname, con.confdeltype
from pg_constraint con
    join pg_class src on src.oid = con.conrelid
where con.contype = 'f' and con.confrelid = %s
order by src.relname, con.conname
"""

_ACCIONES_FK = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE",
                "n": "SET NULL", "d": "SET DEFAULT"}


@dataclass
class Trigger:
    nombre: str
    momento: str
    eventos: list
    por_fila: bool
    activo: bool
    funcion: str
    columnas: list

    def se_dispara(self, operacion, columnas=()):
        """True si el trigger salta con esta operación y columnas."""
        if not self.activo or operacion not in self.eventos:
            return False
        if operacion == "UPDATE" and self.columnas:
            return bool(set(self.columnas) & set(columnas))
        return True

    def describir(self):
        eventos = " OR ".join(
            f"UPDATE OF {', '.join(self.columnas)}"
            if e == "UPDATE" and self.columnas else e
            for e in self.eventos)
        nivel = "FOR EACH ROW" if self.por_fila else "FOR EACH STATEMENT"
        return (f"{self.nombre}: {self.momento} {eventos} {nivel} → "
                f"{self.funcion}()")


@dataclass
class Tabla:
    nombre: str
    existe: bool
    filas_estimadas: int = 0
    clave_primaria: list = field(default_factory=list)
    triggers: list = field(default_factory=list)
    # (restricción, tabla que la referencia, acción ON DELETE)
    referencias_entrantes: list = field(default_factory=list)
    es_vista: bool = False
    # Nombre cualificado y entrecomillado, listo para usar en SQL.
    nombre_sql: str = ""
    columnas: list = field(default_factory=list)
    # Columnas que admiten INSERT (sin las generadas).
    columnas_insertables: list = field(default_factory=list)


def _trigger(fila):
    nombre, tipo, habilitado, funcion, columnas = fila
    if tipo & _TG_INSTEAD:
        momento = "INSTEAD OF"
    elif tipo & _TG_BEFORE:
        momento = "BEFORE"
    else:
        momento = "AFTER"
    eventos = [evento for bit, evento in (
        (_TG_INSERT, "INSERT"), (_TG_UPDATE, "UPDATE"),
        (_TG_DELETE, "DELETE"), (_TG_TRUNCATE, "TRUNCATE"),
    ) if tipo & bit]
    return Trigger(
        nombre=nombre,
        momento=momento,
        eventos=eventos,
        por_fila=bool(tipo & _TG_ROW),
        activo=habilitado != "D",
        funcion=funcion,
        columnas=list(columnas or []),
    )


def leer_tabla(conexion, nombre):
    """Describe una tabla. Si no existe, devuelve `Tabla(existe=False)`."""
    with conexion.cursor() as cur:
        cur.execute(_SQL_TABLA, (nombre,))
        fila = cur.fetchone()
        if fila is None:
            return Tabla(nombre=nombre, existe=False)
        oid, nombre_sql, filas, relkind = fila
        tabla = Tabla(nombre=nombre, existe=True, filas_estimadas=filas,
                      es_vista=relkind in ("v", "m"), nombre_sql=nombre_sql)
        cur.execute(_SQL_COLUMNAS, (oid,))
        for columna, generada in cur.fetchall():
            tabla.columnas.append(columna)
            if not generada:
                tabla.columnas_insertables.append(columna)
        cur.execute(_SQL_CLAVE, (oid,))
        tabla.clave_primaria = [f[0] for f in cur.fetchall()]
        cur.execute(_SQL_TRIGGERS, (oid,))
        tabla.triggers = [_trigger(f) for f in cur.fetchall()]
        cur.execute(_SQL_FK_ENTRANTES, (oid,))
        tabla.referencias_entrantes = [
            (con, origen, _ACCIONES_FK.get(accion, accion))
            for con, origen, accion in cur.fetchall()]
    return tabla


def leer_tablas(conexion, nombres):
    """Describe varias tablas; devuelve un diccionario por nombre."""
    return {nombre: leer_tabla(conexion, nombre) for nombre in nombres}
