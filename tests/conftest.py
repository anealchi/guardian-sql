"""Base de datos de pruebas.

Usa GUARDIAN_TEST_DSN si está definida (debe apuntar a un servidor donde se
pueda crear y borrar la base `guardian_pruebas`). Si no, arranca el
PostgreSQL portátil de `.pg/` con herramientas/pg_local.py. Si no hay
ninguno, las pruebas que necesitan base de datos se saltan.
"""

import os
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
DATOS = Path(__file__).resolve().parent / "datos"
SCRIPTS_REALES = RAIZ.parent / "operaciones-sql-produccion"
BASE_PRUEBAS = "guardian_pruebas"

sys.path.insert(0, str(RAIZ / "herramientas"))


def _dsn_servidor():
    if os.environ.get("GUARDIAN_TEST_DSN"):
        return os.environ["GUARDIAN_TEST_DSN"]
    import pg_local

    if not pg_local.disponible():
        return None
    return pg_local.iniciar()


def _con_base(dsn, base):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    partes = conninfo_to_dict(dsn)
    partes["dbname"] = base
    return make_conninfo(**partes)


@pytest.fixture(scope="session")
def servidor():
    dsn = _dsn_servidor()
    if dsn is None:
        pytest.skip("No hay PostgreSQL para las pruebas.")
    return dsn


@pytest.fixture
def dsn(servidor):
    """Base `guardian_pruebas` recién creada con el esquema de prueba."""
    import psycopg

    with psycopg.connect(servidor, autocommit=True) as conexion:
        conexion.execute(f"DROP DATABASE IF EXISTS {BASE_PRUEBAS} "
                         "WITH (FORCE)")
        conexion.execute(f"CREATE DATABASE {BASE_PRUEBAS}")
    dsn_pruebas = _con_base(servidor, BASE_PRUEBAS)
    esquema = (DATOS / "esquema_prueba.sql").read_text(encoding="utf-8")
    with psycopg.connect(dsn_pruebas, autocommit=True) as conexion:
        conexion.execute(esquema)
    return dsn_pruebas


@pytest.fixture
def script_real():
    """Lee un script de operaciones-sql-produccion o salta la prueba."""
    def leer(nombre):
        ruta = SCRIPTS_REALES / nombre
        if not ruta.exists():
            pytest.skip(f"No está {ruta}.")
        return ruta.read_text(encoding="utf-8-sig")
    return leer
