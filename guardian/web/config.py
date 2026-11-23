"""Configuración del servidor web (`guardian.toml`).

Las conexiones viven solo en este archivo (o en variables de entorno): ni
se guardan en la base de datos de la aplicación ni se envían al navegador.

    [web]
    base_datos = "guardian-web.db"   # relativa al archivo de configuración
    minutos_sesion = 480

    [conexiones.staging]
    dsn = "postgresql://guardian@staging/base"
    simular = true                    # analizar con --simular (bloquea filas)
    ignorar_tablas = ["parte_trabajo_tarea_log"]

    [conexiones.produccion]
    dsn_env = "GUARDIAN_DSN_PRODUCCION"  # la cadena se lee de esa variable
    simular = false
    lock_timeout = "3s"
    statement_timeout = "60s"
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ErrorConfig(Exception):
    pass


@dataclass
class Conexion:
    nombre: str
    dsn: str
    simular: bool = False
    ignorar_tablas: list = field(default_factory=list)
    statement_timeout: str = "60s"
    lock_timeout: str = "3s"

    def publica(self):
        """Lo que puede ver el navegador (sin la cadena de conexión)."""
        return {"nombre": self.nombre, "simular": self.simular,
                "ignorar_tablas": self.ignorar_tablas}


@dataclass
class Config:
    base_datos: Path
    conexiones: dict
    minutos_sesion: int = 480


def _conexion(nombre, datos):
    if "dsn" in datos:
        dsn = datos["dsn"]
    elif "dsn_env" in datos:
        dsn = os.environ.get(datos["dsn_env"], "")
        if not dsn:
            raise ErrorConfig(f"Conexión '{nombre}': la variable "
                              f"{datos['dsn_env']} no está definida.")
    else:
        raise ErrorConfig(f"Conexión '{nombre}': falta dsn o dsn_env.")
    ignorar = datos.get("ignorar_tablas", [])
    if not isinstance(ignorar, list):
        raise ErrorConfig(f"Conexión '{nombre}': ignorar_tablas debe ser "
                          "una lista.")
    return Conexion(
        nombre=nombre, dsn=dsn, simular=bool(datos.get("simular", False)),
        ignorar_tablas=[str(t) for t in ignorar],
        statement_timeout=str(datos.get("statement_timeout", "60s")),
        lock_timeout=str(datos.get("lock_timeout", "3s")))


def cargar(ruta):
    ruta = Path(ruta)
    try:
        datos = tomllib.loads(ruta.read_text(encoding="utf-8"))
    except OSError as error:
        raise ErrorConfig(f"No se puede leer {ruta}: {error.strerror}")
    except tomllib.TOMLDecodeError as error:
        raise ErrorConfig(f"{ruta}: {error}")
    web = datos.get("web", {})
    base = Path(web.get("base_datos", "guardian-web.db"))
    if not base.is_absolute():
        base = ruta.resolve().parent / base
    conexiones = {nombre: _conexion(nombre, valores)
                  for nombre, valores in datos.get("conexiones", {}).items()}
    if not conexiones:
        raise ErrorConfig(f"{ruta}: no hay ninguna conexión en "
                          "[conexiones.*].")
    return Config(base_datos=base, conexiones=conexiones,
                  minutos_sesion=int(web.get("minutos_sesion", 480)))
