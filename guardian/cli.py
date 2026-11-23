"""Línea de comandos de SQL Guardian.

    guardian analizar script.sql [--dsn ...] [--simular]
    guardian ejecutar script.sql [--dsn ...]
    guardian deshacer ID [--dsn ...] [--solo-sql]
    guardian historial [--dsn ...]
    guardian web [--config guardian.toml] [--host ...] [--puerto ...]
    guardian usuario crear NOMBRE --rol admin [--config guardian.toml]
"""

import argparse
import os
import sys
import threading
from pathlib import Path

from guardian.analisis import ALTO, CRITICO, NIVELES, analizar
from guardian.informe import markdown, texto_consola
from guardian.sentencias import ErrorSintaxis, leer_script

# Códigos de salida, pensados para usarlo en CI.
SALIDA_OK = 0
SALIDA_RIESGOS = 1
SALIDA_ERROR = 2

# `analizar`: 1 = riesgos altos. `ejecutar`/`deshacer`: 1 = no aplicado.
SALIDA_NO_APLICADO = 1


def _opcion_dsn(parser, obligatoria_texto):
    parser.add_argument(
        "--dsn", default=os.environ.get("GUARDIAN_DSN"),
        help="conexión a PostgreSQL, p. ej. 'postgresql://usuario@host/"
             f"base'. Por defecto, GUARDIAN_DSN. {obligatoria_texto}")


def _opciones_tiempo(parser, confirmacion=False):
    parser.add_argument("--statement-timeout", default="60s",
                        help="tiempo máximo por sentencia (por defecto 60s)")
    parser.add_argument("--lock-timeout", default="3s",
                        help="espera máxima por un bloqueo (por defecto 3s)")
    if confirmacion:
        parser.add_argument(
            "--confirmar", action="store_true",
            help="hace COMMIT sin preguntar (para automatizar)")
        parser.add_argument(
            "--tiempo-confirmacion", type=int, default=120, metavar="SEG",
            help="segundos para escribir COMMIT; pasado ese tiempo se hace "
                 "ROLLBACK (por defecto 120). Mientras tanto las filas "
                 "siguen bloqueadas")


def _argumentos(argv):
    parser = argparse.ArgumentParser(
        prog="guardian",
        description="Revisa scripts SQL de PostgreSQL antes de ejecutarlos "
                    "en producción.")
    sub = parser.add_subparsers(dest="orden", required=True)

    a = sub.add_parser(
        "analizar", help="analiza un script y calcula su impacto",
        description="Analiza un script sin ejecutarlo. Con --dsn (o la "
                    "variable GUARDIAN_DSN) lee el esquema y calcula cuántas "
                    "filas cambia cada sentencia; siempre termina con "
                    "ROLLBACK.")
    a.add_argument("script", type=Path, help="archivo .sql")
    _opcion_dsn(a, "Sin conexión solo hace el análisis estático.")
    a.add_argument("--sin-conexion", action="store_true",
                   help="ignora GUARDIAN_DSN y solo hace el análisis "
                        "estático")
    a.add_argument("--simular", action="store_true",
                   help="ejecuta el script completo dentro de una "
                        "transacción y lo deshace. Da números exactos y "
                        "los cambios de los triggers, pero bloquea filas "
                        "mientras dura: úsalo en staging o con cuidado")
    a.add_argument("--markdown", "-m", type=Path, metavar="ARCHIVO",
                   help="guarda también el informe en Markdown")
    _opciones_tiempo(a)

    e = sub.add_parser(
        "ejecutar", help="ejecuta un script con respaldo para deshacerlo",
        description="Ejecuta el script en una sola transacción, guardando "
                    "antes las filas que cambia en el esquema "
                    "guardian_backup. Muestra el resultado y solo hace "
                    "COMMIT si escribes COMMIT.")
    e.add_argument("script", type=Path, help="archivo .sql")
    _opcion_dsn(e, "Obligatoria.")
    e.add_argument("--forzar", action="store_true",
                   help="ejecuta también las sentencias que no se pueden "
                        "respaldar y las que tienen riesgos críticos")
    e.add_argument("--permitir-indirectos", action="store_true",
                   help="permite confirmar aunque triggers o cascadas "
                        "cambien filas que el respaldo no cubre")
    e.add_argument("--ignorar-tabla", action="append", default=[],
                   metavar="TABLA",
                   help="tabla cuyos cambios indirectos no importan (p. ej. "
                        "una tabla de log). Se puede repetir")
    _opciones_tiempo(e, confirmacion=True)

    d = sub.add_parser(
        "deshacer", help="deshace una ejecución con su respaldo",
        description="Restaura las filas que cambió una ejecución. Solo "
                    "toca las que siguen como las dejó; las demás se "
                    "omiten.")
    d.add_argument("ejecucion", help="identificador (ver `historial`)")
    _opcion_dsn(d, "Obligatoria.")
    d.add_argument("--solo-sql", action="store_true",
                   help="muestra el SQL que se ejecutaría, sin ejecutarlo")
    _opciones_tiempo(d, confirmacion=True)

    h = sub.add_parser("historial", help="lista las últimas ejecuciones")
    _opcion_dsn(h, "Obligatoria.")
    h.add_argument("--limite", type=int, default=20,
                   help="número de ejecuciones (por defecto 20)")

    config = os.environ.get("GUARDIAN_CONFIG", "guardian.toml")
    w = sub.add_parser(
        "web", help="arranca la aplicación web de aprobaciones",
        description="Servidor web para pedir, aprobar, ejecutar y deshacer "
                    "scripts con registro de auditoría.")
    w.add_argument("--config", type=Path, default=Path(config),
                   help="archivo de configuración (por defecto "
                        "guardian.toml o GUARDIAN_CONFIG)")
    w.add_argument("--host", default="127.0.0.1",
                   help="dirección en la que escucha (por defecto solo este "
                        "equipo). Para el equipo, ponlo detrás de un proxy "
                        "con HTTPS")
    w.add_argument("--puerto", type=int, default=8000)

    u = sub.add_parser("usuario", help="gestiona usuarios de la web")
    usub = u.add_subparsers(dest="accion", required=True)
    uc = usub.add_parser("crear", help="crea un usuario (pide la "
                                       "contraseña)")
    uc.add_argument("nombre")
    uc.add_argument("--rol", required=True,
                    choices=("solicitante", "aprobador", "admin"))
    uc.add_argument("--nombre-completo", default="")
    uc.add_argument("--config", type=Path, default=Path(config))
    ul = usub.add_parser("listar", help="lista los usuarios")
    ul.add_argument("--config", type=Path, default=Path(config))
    return parser.parse_args(argv)


def _leer(ruta):
    """Devuelve (fuente, sentencias) o None si hay error (ya informado)."""
    try:
        fuente = ruta.read_text(encoding="utf-8-sig")
    except OSError as error:
        print(f"No se puede leer {ruta}: {error.strerror}", file=sys.stderr)
        return None
    try:
        return fuente, leer_script(fuente)
    except ErrorSintaxis as error:
        print(f"{ruta}: error de sintaxis. {error}", file=sys.stderr)
        return None


def pedir_confirmacion(segundos):
    """True solo si el usuario escribe COMMIT antes de `segundos`."""
    if not sys.stdin or not sys.stdin.isatty():
        print("La entrada no es interactiva: se hace ROLLBACK (usa "
              "--confirmar para aplicar sin preguntar).")
        return False
    respuesta = []

    def leer():
        try:
            respuesta.append(input())
        except EOFError:
            respuesta.append("")

    print(f"\nEscribe COMMIT para aplicar los cambios (cualquier otra cosa "
          f"hace ROLLBACK; {segundos} s): ", end="", flush=True)
    hilo = threading.Thread(target=leer, daemon=True)
    hilo.start()
    hilo.join(segundos)
    if not respuesta:
        print(f"\nTiempo agotado ({segundos} s): se hace ROLLBACK.")
        return False
    return respuesta[0].strip().upper() == "COMMIT"


def _confirmador(args):
    if args.confirmar:
        return lambda _resultado: True
    return lambda _resultado: pedir_confirmacion(args.tiempo_confirmacion)


def _analizar(args):
    leido = _leer(args.script)
    if leido is None:
        return SALIDA_ERROR
    _fuente, sentencias = leido

    hallazgos = analizar(sentencias)
    impacto = None
    if args.dsn and not args.sin_conexion:
        # Import diferido: el análisis estático no necesita psycopg.
        import psycopg

        from guardian.impacto import calcular

        modo = "simulacion" if args.simular else "conteo"
        try:
            impacto = calcular(args.dsn, sentencias, modo=modo,
                               statement_timeout=args.statement_timeout,
                               lock_timeout=args.lock_timeout)
        except psycopg.OperationalError as error:
            print(f"No se puede conectar a PostgreSQL: {error}",
                  file=sys.stderr)
            return SALIDA_ERROR
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return SALIDA_ERROR
        hallazgos = sorted(
            hallazgos + impacto.hallazgos,
            key=lambda h: (NIVELES.index(h.nivel), h.linea or 0))
    elif args.simular:
        print("--simular necesita una conexión (--dsn o GUARDIAN_DSN).",
              file=sys.stderr)
        return SALIDA_ERROR

    nombre = args.script.name
    print(texto_consola(nombre, sentencias, hallazgos, impacto))
    if args.markdown:
        args.markdown.write_text(
            markdown(nombre, sentencias, hallazgos, impacto),
            encoding="utf-8")
        print(f"\nInforme guardado en {args.markdown}")

    graves = any(h.nivel in (CRITICO, ALTO) for h in hallazgos)
    return SALIDA_RIESGOS if graves else SALIDA_OK


def _ejecutar(args):
    import psycopg

    from guardian.ejecucion import ErrorEjecucion, ejecutar
    from guardian.informe import texto_ejecucion

    leido = _leer(args.script)
    if leido is None:
        return SALIDA_ERROR
    fuente, sentencias = leido
    confirmar = _confirmador(args)
    mostrado = []

    def mostrar_y_confirmar(ejecucion):
        print(texto_ejecucion(ejecucion))
        mostrado.append(True)
        return confirmar(ejecucion)

    try:
        ejecucion = ejecutar(
            args.dsn, args.script.name, fuente, sentencias,
            mostrar_y_confirmar, forzar=args.forzar,
            permitir_indirectos=args.permitir_indirectos,
            ignorar=args.ignorar_tabla,
            statement_timeout=args.statement_timeout,
            lock_timeout=args.lock_timeout,
            tiempo_confirmacion=args.tiempo_confirmacion)
    except psycopg.OperationalError as error:
        print(f"No se puede conectar a PostgreSQL: {error}", file=sys.stderr)
        return SALIDA_ERROR
    except ErrorEjecucion as error:
        print(f"Error, se ha hecho ROLLBACK de todo. {error}",
              file=sys.stderr)
        return SALIDA_ERROR

    if ejecucion.aplicada:
        print(f"\nAplicado. Para deshacerlo: guardian deshacer "
              f"{ejecucion.id}")
        return SALIDA_OK
    if not mostrado:
        # Se rechazó antes de pedir confirmación: falta el resumen.
        print(texto_ejecucion(ejecucion))
    print(f"\nNo aplicado. {ejecucion.motivo}")
    return SALIDA_NO_APLICADO


def _deshacer(args):
    import psycopg

    from guardian.deshacer import ErrorDeshacer, deshacer, script_deshacer
    from guardian.informe import texto_deshacer

    confirmar = _confirmador(args)

    def mostrar_y_confirmar(resultado):
        print(texto_deshacer(resultado))
        return confirmar(resultado)

    try:
        if args.solo_sql:
            print(script_deshacer(args.dsn, args.ejecucion), end="")
            return SALIDA_OK
        resultado = deshacer(
            args.dsn, args.ejecucion, mostrar_y_confirmar,
            statement_timeout=args.statement_timeout,
            lock_timeout=args.lock_timeout,
            tiempo_confirmacion=args.tiempo_confirmacion)
    except psycopg.OperationalError as error:
        print(f"No se puede conectar a PostgreSQL: {error}", file=sys.stderr)
        return SALIDA_ERROR
    except (ErrorDeshacer, ValueError) as error:
        print(f"No se puede deshacer. {error}", file=sys.stderr)
        return SALIDA_ERROR
    if resultado.aplicado:
        print(f"\nEjecución {resultado.id} deshecha.")
        return SALIDA_OK
    print(f"\nNo aplicado. {resultado.motivo}")
    return SALIDA_NO_APLICADO


def _historial(args):
    import psycopg

    from guardian.deshacer import historial
    from guardian.informe import texto_historial

    try:
        print(texto_historial(historial(args.dsn, args.limite)))
    except psycopg.OperationalError as error:
        print(f"No se puede conectar a PostgreSQL: {error}", file=sys.stderr)
        return SALIDA_ERROR
    return SALIDA_OK


def _cargar_config(ruta):
    from guardian.web.config import ErrorConfig, cargar

    try:
        return cargar(ruta)
    except ErrorConfig as error:
        print(f"Error en la configuración. {error}", file=sys.stderr)
        return None


def _web(args):
    try:
        import uvicorn

        from guardian.web.app import crear_app
    except ImportError:
        print("Faltan las dependencias web: pip install -e .[web]",
              file=sys.stderr)
        return SALIDA_ERROR
    config = _cargar_config(args.config)
    if config is None:
        return SALIDA_ERROR
    app = crear_app(config)
    if not app.state.almacen.hay_usuarios():
        print("Aún no hay usuarios. Crea el primero con:\n"
              "  guardian usuario crear NOMBRE --rol admin",
              file=sys.stderr)
        return SALIDA_ERROR
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("Aviso: escuchando fuera de este equipo sin HTTPS. Pon un "
              "proxy con HTTPS delante (las contraseñas y la cookie de "
              "sesión viajan en cada petición).", file=sys.stderr)
    print(f"SQL Guardian en http://{args.host}:{args.puerto}  "
          f"(datos: {config.base_datos})")
    uvicorn.run(app, host=args.host, port=args.puerto, log_level="warning")
    return SALIDA_OK


def _usuario(args):
    import getpass

    from guardian.web.almacen import Almacen

    config = _cargar_config(args.config)
    if config is None:
        return SALIDA_ERROR
    almacen = Almacen(config.base_datos)
    if args.accion == "listar":
        for u in almacen.usuarios():
            estado = "" if u["activo"] else "  (desactivado)"
            print(f"{u['nombre']:<20} {u['rol']:<12} "
                  f"{u['nombre_completo']}{estado}")
        return SALIDA_OK
    clave = os.environ.get("GUARDIAN_CLAVE_NUEVA")
    if clave is None:
        clave = getpass.getpass("Contraseña (mínimo 10 caracteres): ")
        if getpass.getpass("Repítela: ") != clave:
            print("Las contraseñas no coinciden.", file=sys.stderr)
            return SALIDA_ERROR
    try:
        almacen.crear_usuario(args.nombre, clave, args.rol,
                              args.nombre_completo)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return SALIDA_ERROR
    with almacen.transaccion() as db:
        almacen.registrar(db, "consola", "crear_usuario", None,
                          {"usuario": args.nombre, "rol": args.rol})
    print(f"Usuario {args.nombre} creado con el rol {args.rol}.")
    return SALIDA_OK


def main(argv=None):
    # La consola de Windows puede no usar UTF-8 por defecto.
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8", errors="replace")
    args = _argumentos(argv)
    if args.orden in ("web", "usuario"):
        return _web(args) if args.orden == "web" else _usuario(args)
    if args.orden != "analizar" and not args.dsn:
        print(f"`guardian {args.orden}` necesita una conexión (--dsn o "
              "GUARDIAN_DSN).", file=sys.stderr)
        return SALIDA_ERROR
    ordenes = {"analizar": _analizar, "ejecutar": _ejecutar,
               "deshacer": _deshacer, "historial": _historial}
    return ordenes[args.orden](args)


if __name__ == "__main__":
    sys.exit(main())
