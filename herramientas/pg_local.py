"""PostgreSQL portátil para desarrollo y pruebas.

Usa los binarios de `.pg/pgsql/bin` (ignorados por git) y guarda los datos
en `.pg/datos`. Solo escucha en 127.0.0.1.

    python herramientas/pg_local.py iniciar
    python herramientas/pg_local.py parar
    python herramientas/pg_local.py dsn
"""

import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
BINARIOS = RAIZ / ".pg" / "pgsql" / "bin"
DATOS = RAIZ / ".pg" / "datos"
REGISTRO = RAIZ / ".pg" / "postgres.log"
PUERTO = int(os.environ.get("GUARDIAN_PG_PUERTO", "54329"))
USUARIO = "guardian"


def disponible():
    """True si están los binarios de PostgreSQL portátil."""
    return (BINARIOS / _exe("pg_ctl")).exists()


def _exe(nombre):
    return nombre + (".exe" if os.name == "nt" else "")


def _ejecutar(nombre, *argumentos):
    return subprocess.run([str(BINARIOS / _exe(nombre)), *argumentos],
                          capture_output=True, text=True)


def dsn(base="postgres"):
    return f"postgresql://{USUARIO}@127.0.0.1:{PUERTO}/{base}"


def en_marcha():
    return _ejecutar("pg_ctl", "status", "-D", str(DATOS)).returncode == 0


def iniciar():
    """Crea el clúster si no existe y lo arranca. Devuelve el DSN."""
    if not disponible():
        raise RuntimeError(f"No están los binarios en {BINARIOS}.")
    if not (DATOS / "PG_VERSION").exists():
        r = _ejecutar("initdb", "-D", str(DATOS), "-U", USUARIO,
                      "--auth=trust", "--encoding=UTF8", "--locale=C")
        if r.returncode != 0:
            raise RuntimeError(f"initdb falló:\n{r.stderr}")
    if not en_marcha():
        opciones = f"-p {PUERTO} -c listen_addresses=127.0.0.1"
        # Sin capturar la salida: el servidor hereda los descriptores y
        # subprocess se quedaría esperando a que se cierren.
        r = subprocess.run(
            [str(BINARIOS / _exe("pg_ctl")), "start", "-w", "-D",
             str(DATOS), "-l", str(REGISTRO), "-o", opciones],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        if r.returncode != 0:
            raise RuntimeError(f"pg_ctl start falló; revisa {REGISTRO}.")
    return dsn()


def parar():
    if en_marcha():
        _ejecutar("pg_ctl", "stop", "-m", "fast", "-D", str(DATOS))


def main():
    orden = sys.argv[1] if len(sys.argv) > 1 else "iniciar"
    if orden == "iniciar":
        print(iniciar())
    elif orden == "parar":
        parar()
    elif orden == "dsn":
        print(dsn())
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
