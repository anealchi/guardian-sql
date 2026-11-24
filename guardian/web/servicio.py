"""Reglas del flujo de solicitudes, independientes de HTTP.

    pendiente ──aprobar──▶ aprobada ──ejecutar──▶ ejecutando ─┬─▶ ejecutada
        │  ▲                  │                               ├─▶ no_aplicada
        │  └──reanalizar──────┤                               └─▶ fallida
        ├──rechazar──▶ rechazada              ejecutada ──deshacer──▶ deshecha
        ├──cancelar──▶ cancelada
        └──editar───▶ pendiente (solo quien la pidió)

Si el servidor se detiene a mitad de una ejecución, la solicitud se queda
en 'ejecutando'; `recuperar()` mira en PostgreSQL si hubo COMMIT y la pasa
a 'ejecutada' o 'no_aplicada' (o 'deshecha' si se deshizo por la CLI).

Reglas principales:

- Quien aprueba es un aprobador o admin. Puede aprobar sus propias
  solicitudes; el evento lo marca con `propia` para que se vea en la
  auditoría.
- Solo se aprueba con el impacto calculado contra la base de datos, y lo
  que se aprueba es exactamente ese script (se comprueba su hash) con esas
  opciones.
- Mientras está pendiente, quien la pidió puede editarla; si cambia el
  script o la conexión, se vuelve a analizar. Como el aprobador podría
  tener abierta la versión anterior, `aprobar()` acepta la `version()` que
  vio y no aprueba si ya no es la actual.
- Al ejecutar, si una sentencia cambia un número de filas distinto del que
  vio el aprobador (en los recuentos exactos), no se hace COMMIT: hay que
  reanalizar y volver a aprobar.
- Todo cambio de estado deja un evento en la cadena de auditoría, en la
  misma transacción de SQLite.
"""

import hashlib
import json

import psycopg

from guardian.analisis import NIVELES, analizar
from guardian.informe import informe_json
from guardian.sentencias import ErrorSintaxis, leer_script
from guardian.web.almacen import ahora as _ahora

TAMANO_MAXIMO = 1_000_000
_MODIFICACIONES = ("UPDATE", "DELETE", "INSERT")
# Autor de lo que hace el servidor por su cuenta (recuperar al arrancar),
# como "consola" en los eventos de la CLI.
SISTEMA = {"id": None, "nombre": "servidor", "rol": "admin"}


class ErrorFlujo(Exception):
    estado_http = 400


class Prohibido(ErrorFlujo):
    estado_http = 403


class NoEncontrado(ErrorFlujo):
    estado_http = 404


class Conflicto(ErrorFlujo):
    estado_http = 409


def _hash(sql):
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _es_aprobador(usuario):
    return usuario["rol"] in ("aprobador", "admin")


def version(solicitud):
    """Huella de lo que se aprueba: script, conexión, opciones e informe.

    Cambia al editar o reanalizar, no al comentar. No se usa la fecha de
    actualización porque solo tiene precisión de segundos.
    """
    contenido = json.dumps(
        [solicitud["hash"], solicitud["conexion"], solicitud["opciones"],
         solicitud["informe"]], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(contenido.encode("utf-8")).hexdigest()[:16]


def filas_aprobadas(informe):
    """{índice de sentencia: filas} de los recuentos exactos aprobados."""
    impacto = (informe or {}).get("impacto") or {}
    return {r["indice"]: r["filas"] for r in impacto.get("resultados", [])
            if r["operacion"] in _MODIFICACIONES
            and r["precision"] == "exacto" and r["filas"] is not None}


class Servicio:
    def __init__(self, config, almacen):
        self.config = config
        self.almacen = almacen

    # --- Utilidades -----------------------------------------------------

    def _conexion(self, nombre):
        try:
            return self.config.conexiones[nombre]
        except KeyError:
            raise ErrorFlujo(f"No existe la conexión '{nombre}'.")

    def _cargar(self, db, solicitud_id):
        solicitud = self.almacen.solicitud(solicitud_id, db)
        if solicitud is None:
            raise NoEncontrado(f"No existe la solicitud {solicitud_id}.")
        return solicitud

    def _transicion(self, db, solicitud, desde, **campos):
        if not self.almacen.actualizar_solicitud(db, solicitud["id"],
                                                 desde=desde, **campos):
            raise Conflicto(f"La solicitud está en estado "
                            f"'{solicitud['estado']}': no se puede hacer "
                            "eso ahora.")

    def analizar(self, nombre_conexion, sql):
        """Informe (diccionario) de un script contra una conexión."""
        from guardian.impacto import calcular

        conexion = self._conexion(nombre_conexion)
        try:
            sentencias = leer_script(sql)
        except ErrorSintaxis as error:
            raise ErrorFlujo(f"Error de sintaxis. {error}")
        if not sentencias:
            raise ErrorFlujo("El script está vacío.")
        hallazgos = analizar(sentencias)
        impacto = error = None
        try:
            impacto = calcular(
                conexion.dsn, sentencias,
                modo="simulacion" if conexion.simular else "conteo",
                statement_timeout=conexion.statement_timeout,
                lock_timeout=conexion.lock_timeout)
            hallazgos = sorted(
                hallazgos + impacto.hallazgos,
                key=lambda h: (NIVELES.index(h.nivel), h.linea or 0))
        except (psycopg.Error, ValueError) as e:
            error = (f"No se pudo calcular el impacto en "
                     f"'{nombre_conexion}': {str(e).strip()}")
        return informe_json(sentencias, hallazgos, impacto, error)

    # --- Acciones -------------------------------------------------------

    def _datos(self, titulo, motivo, conexion, script_nombre, sql,
               ignorar_tablas, permitir_indirectos, forzar):
        """Valida y normaliza lo que se rellena al crear o editar."""
        titulo = (titulo or "").strip()
        if not titulo:
            raise ErrorFlujo("Falta el título.")
        if not sql or not sql.strip():
            raise ErrorFlujo("Falta el script.")
        if len(sql.encode("utf-8")) > TAMANO_MAXIMO:
            raise ErrorFlujo("El script supera 1 MB.")
        datos_conexion = self._conexion(conexion)
        ignorar = sorted({t.strip() for t in
                          list(ignorar_tablas) + datos_conexion.ignorar_tablas
                          if t and t.strip()})
        return {"titulo": titulo[:200], "motivo": (motivo or "").strip(),
                "conexion": conexion,
                "script_nombre": (script_nombre or "script.sql")[:200],
                "sql": sql, "hash": _hash(sql),
                "opciones": {"ignorar_tablas": ignorar,
                             "permitir_indirectos": bool(permitir_indirectos),
                             "forzar": bool(forzar)}}

    def crear(self, usuario, titulo, motivo, conexion, script_nombre, sql,
              ignorar_tablas=(), permitir_indirectos=False, forzar=False):
        datos = self._datos(titulo, motivo, conexion, script_nombre, sql,
                            ignorar_tablas, permitir_indirectos, forzar)
        informe = self.analizar(conexion, sql)
        with self.almacen.transaccion() as db:
            solicitud_id = self.almacen.insertar_solicitud(db, dict(
                datos, estado="pendiente", informe=informe,
                informe_fecha=_ahora(), solicitante_id=usuario["id"]))
            self.almacen.registrar(db, usuario, "crear", solicitud_id, {
                "hash": datos["hash"], "conexion": conexion,
                "opciones": datos["opciones"],
                "veredicto": informe["veredicto"]["nivel"],
                "filas_previstas": informe["filas_previstas"]})
            return self.almacen.solicitud(solicitud_id, db)

    def editar(self, usuario, solicitud_id, titulo, motivo, conexion,
               script_nombre, sql, ignorar_tablas=(),
               permitir_indirectos=False, forzar=False):
        """Cambia una solicitud pendiente. Solo quien la pidió.

        Si cambia el script o la conexión, se vuelve a analizar (fuera de
        la transacción, como en `reanalizar`). El evento guarda qué campos
        cambiaron y, si cambió el script, el hash anterior y el nuevo.
        """
        with self.almacen.transaccion() as db:
            solicitud = self._cargar(db, solicitud_id)
        if usuario["id"] != solicitud["solicitante_id"]:
            raise Prohibido("Solo quien la pidió puede editarla.")
        if solicitud["estado"] != "pendiente":
            raise Conflicto("Solo se puede editar una solicitud pendiente "
                            "de aprobar.")
        datos = self._datos(titulo, motivo, conexion, script_nombre, sql,
                            ignorar_tablas, permitir_indirectos, forzar)
        cambios = sorted(
            clave for clave in ("titulo", "motivo", "conexion",
                                "script_nombre", "sql", "opciones")
            if datos[clave] != solicitud[clave])
        if not cambios:
            return solicitud
        if "sql" in cambios or "conexion" in cambios:
            informe = self.analizar(conexion, sql)
            datos.update(informe=informe, informe_fecha=_ahora())
        with self.almacen.transaccion() as db:
            actual = self._cargar(db, solicitud_id)
            # Si mientras se analizaba alguien la editó o la reanalizó, se
            # rechaza: si no, se perderían sus cambios sin que lo sepa.
            if version(actual) != version(solicitud):
                raise Conflicto("La solicitud ha cambiado mientras la "
                                "editabas. Vuelve a abrirla.")
            self._transicion(db, actual, ("pendiente",), **datos)
            detalle = {"cambios": cambios}
            if "sql" in cambios:
                detalle.update(hash_anterior=solicitud["hash"],
                               hash=datos["hash"])
            if "opciones" in cambios:
                detalle["opciones"] = datos["opciones"]
            if "informe" in datos:
                detalle.update(
                    veredicto=datos["informe"]["veredicto"]["nivel"],
                    filas_previstas=datos["informe"]["filas_previstas"])
            self.almacen.registrar(db, usuario, "editar", solicitud_id,
                                   detalle)
            return self.almacen.solicitud(solicitud_id, db)

    def reanalizar(self, usuario, solicitud_id):
        with self.almacen.transaccion() as db:
            solicitud = self._cargar(db, solicitud_id)
        if (usuario["id"] != solicitud["solicitante_id"]
                and not _es_aprobador(usuario)):
            raise Prohibido("Solo quien la pidió o un aprobador puede "
                            "reanalizarla.")
        # El análisis puede tardar: se hace fuera de la transacción.
        informe = self.analizar(solicitud["conexion"], solicitud["sql"])
        with self.almacen.transaccion() as db:
            solicitud = self._cargar(db, solicitud_id)
            self._transicion(
                db, solicitud, ("pendiente", "aprobada", "no_aplicada"),
                estado="pendiente", informe=informe,
                informe_fecha=_ahora(), aprobador_id=None, aprobada_en=None)
            self.almacen.registrar(db, usuario, "reanalizar", solicitud_id, {
                "veredicto": informe["veredicto"]["nivel"],
                "filas_previstas": informe["filas_previstas"],
                "aprobacion_anulada": solicitud["estado"] == "aprobada"})
            return self.almacen.solicitud(solicitud_id, db)

    def aprobar(self, usuario, solicitud_id, comentario="", vista=None):
        """Aprueba. `vista` es la `version()` que tenía abierta el aprobador:
        si se pasa y ya no es la actual, no se aprueba."""
        if not _es_aprobador(usuario):
            raise Prohibido("Solo un aprobador puede aprobar.")
        with self.almacen.transaccion() as db:
            solicitud = self._cargar(db, solicitud_id)
            if vista is not None and vista != version(solicitud):
                raise Conflicto("La solicitud ha cambiado desde que la "
                                "abriste. Revisa la versión actual antes "
                                "de aprobarla.")
            informe = solicitud["informe"] or {}
            if informe.get("impacto") is None:
                raise Conflicto("No hay impacto calculado contra la base de "
                                "datos: reanaliza antes de aprobar.")
            if (informe["veredicto"]["nivel"] == "bloqueado"
                    and not solicitud["opciones"].get("forzar")):
                raise Conflicto("El script tiene riesgos críticos y no se "
                                "pidió ejecutarlo forzado.")
            if _hash(solicitud["sql"]) != solicitud["hash"]:
                raise Conflicto("El script no coincide con su hash.")
            self._transicion(db, solicitud, ("pendiente",),
                             estado="aprobada", aprobador_id=usuario["id"],
                             aprobada_en=_ahora())
            detalle = {"hash": solicitud["hash"],
                       "comentario": comentario.strip(),
                       "filas_aprobadas": filas_aprobadas(informe)}
            if solicitud["solicitante_id"] == usuario["id"]:
                detalle["propia"] = True
            self.almacen.registrar(db, usuario, "aprobar", solicitud_id,
                                   detalle)
            return self.almacen.solicitud(solicitud_id, db)

    def rechazar(self, usuario, solicitud_id, comentario):
        if not _es_aprobador(usuario):
            raise Prohibido("Solo un aprobador puede rechazar.")
        if not (comentario or "").strip():
            raise ErrorFlujo("Explica por qué se rechaza.")
        with self.almacen.transaccion() as db:
            solicitud = self._cargar(db, solicitud_id)
            self._transicion(db, solicitud, ("pendiente", "aprobada"),
                             estado="rechazada")
            self.almacen.registrar(db, usuario, "rechazar", solicitud_id,
                                   {"comentario": comentario.strip()})
            return self.almacen.solicitud(solicitud_id, db)

    def cancelar(self, usuario, solicitud_id, comentario=""):
        with self.almacen.transaccion() as db:
            solicitud = self._cargar(db, solicitud_id)
            if (usuario["id"] != solicitud["solicitante_id"]
                    and usuario["rol"] != "admin"):
                raise Prohibido("Solo quien la pidió puede cancelarla.")
            self._transicion(db, solicitud, ("pendiente", "aprobada"),
                             estado="cancelada")
            self.almacen.registrar(db, usuario, "cancelar", solicitud_id,
                                   {"comentario": (comentario or "").strip()})
            return self.almacen.solicitud(solicitud_id, db)

    def comentar(self, usuario, solicitud_id, comentario):
        if not (comentario or "").strip():
            raise ErrorFlujo("El comentario está vacío.")
        with self.almacen.transaccion() as db:
            self._cargar(db, solicitud_id)
            self.almacen.registrar(db, usuario, "comentar", solicitud_id,
                                   {"comentario": comentario.strip()})
            return self.almacen.solicitud(solicitud_id, db)

    def _comprobar_ejecutor(self, usuario, solicitud):
        if (usuario["id"] != solicitud["solicitante_id"]
                and not _es_aprobador(usuario)):
            raise Prohibido("Solo quien la pidió o un aprobador puede "
                            "ejecutarla.")

    def ejecutar(self, usuario, solicitud_id):
        from guardian.ejecucion import ErrorEjecucion, ejecutar
        from guardian.respaldo import nuevo_id

        with self.almacen.transaccion() as db:
            solicitud = self._cargar(db, solicitud_id)
            self._comprobar_ejecutor(usuario, solicitud)
            if solicitud["estado"] != "aprobada":
                raise Conflicto(f"La solicitud está en estado "
                                f"'{solicitud['estado']}': no se puede "
                                "ejecutar.")
            if _hash(solicitud["sql"]) != solicitud["hash"]:
                raise Conflicto("El script no coincide con su hash.")

        ejecucion_id = nuevo_id()
        iniciada = False

        def empezar(_ejecucion):
            # Se llama con el advisory lock de la ejecución ya tomado: así
            # `recuperar()` nunca ve 'ejecutando' sin poder saber si sigue.
            nonlocal iniciada
            with self.almacen.transaccion() as db:
                actual = self._cargar(db, solicitud_id)
                self._transicion(db, actual, ("aprobada",),
                                 estado="ejecutando",
                                 ejecutor_id=usuario["id"],
                                 ejecucion_id=ejecucion_id)
                self.almacen.registrar(db, usuario, "ejecucion_iniciada",
                                       solicitud_id,
                                       {"hash": solicitud["hash"],
                                        "ejecucion": ejecucion_id})
            iniciada = True

        conexion = self._conexion(solicitud["conexion"])
        opciones = solicitud["opciones"]
        esperadas = filas_aprobadas(solicitud["informe"])
        discrepancias = []

        def confirmar(ejecucion):
            for paso in ejecucion.pasos:
                previstas = esperadas.get(paso.indice)
                if previstas is not None and paso.filas != previstas:
                    discrepancias.append({
                        "linea": paso.linea, "operacion": paso.operacion,
                        "tabla": paso.tabla, "aprobadas": previstas,
                        "reales": paso.filas})
            return not discrepancias

        resultado = {"discrepancias": discrepancias}
        estado = "fallida"
        try:
            ejecucion = ejecutar(
                conexion.dsn, solicitud["script_nombre"], solicitud["sql"],
                leer_script(solicitud["sql"]), confirmar,
                forzar=opciones.get("forzar", False),
                permitir_indirectos=opciones.get("permitir_indirectos",
                                                 False),
                ignorar=opciones.get("ignorar_tablas", []),
                statement_timeout=conexion.statement_timeout,
                lock_timeout=conexion.lock_timeout,
                origen={"solicitud": str(solicitud_id),
                        "solicitado_por": solicitud["solicitante"],
                        "aprobado_por": solicitud["aprobador"]},
                ejecucion_id=ejecucion_id, al_empezar=empezar)
            resultado.update(_resultado_ejecucion(ejecucion))
            if ejecucion.aplicada:
                estado = "ejecutada"
            else:
                estado = "no_aplicada"
                # Los motivos de `ejecucion` hablan de opciones de la CLI;
                # aquí se explican con las casillas del formulario.
                if discrepancias:
                    resultado["motivo"] = (
                        "El número de filas no coincide con el aprobado: "
                        "se ha hecho ROLLBACK. Reanaliza y vuelve a "
                        "aprobar.")
                elif ejecucion.bloqueos and not opciones.get("forzar"):
                    resultado["motivo"] = (
                        "Hay sentencias que no se pueden respaldar y no se "
                        "ha aplicado nada. Para ejecutarlas sin respaldo, "
                        "envía una solicitud nueva marcando «Ejecutar "
                        "también lo que no se puede respaldar».")
                elif ejecucion.sin_respaldo:
                    resultado["motivo"] = (
                        "Hay cambios que el respaldo no cubre (triggers o "
                        "cascadas) y no se ha aplicado nada. Si son "
                        "esperables, envía una solicitud nueva marcando "
                        "«Permitir cambios sin respaldo» o añadiendo esas "
                        "tablas a las que se ignoran.")
        except ErrorEjecucion as error:
            resultado["motivo"] = f"Error, se ha hecho ROLLBACK. {error}"
        except Exception as error:  # noqa: BLE001 - se registra y se informa
            if not iniciada:
                # No llegó a empezar: la solicitud sigue aprobada.
                if isinstance(error, ErrorFlujo):
                    raise
                raise ErrorFlujo(
                    f"No se pudo empezar la ejecución en "
                    f"'{solicitud['conexion']}': {str(error).strip()}")
            resultado["motivo"] = f"Error inesperado: {error}"
        finally:
            if iniciada:
                aplicada = estado == "ejecutada"
                with self.almacen.transaccion() as db:
                    self.almacen.actualizar_solicitud(
                        db, solicitud_id, estado=estado,
                        resultado=resultado, ejecutada_en=_ahora(),
                        ejecucion_id=ejecucion_id if aplicada else None)
                    self.almacen.registrar(
                        db, usuario, f"ejecucion_{estado}", solicitud_id, {
                            "ejecucion": ejecucion_id if aplicada else None,
                            "filas_respaldadas": resultado.get(
                                "filas_respaldadas"),
                            "motivo": resultado.get("motivo"),
                            "discrepancias": discrepancias})
        return self.almacen.solicitud(solicitud_id)

    def recuperar(self, usuario, solicitud_id):
        """Cierra una solicitud que se quedó en 'ejecutando'.

        Pasa si el servidor web se detuvo durante la ejecución. Se mira en
        PostgreSQL qué fue de ella: si sigue en curso, no se toca; si hubo
        COMMIT, queda 'ejecutada'; si no, PostgreSQL ya deshizo la
        transacción y queda 'no_aplicada'.
        """
        from guardian.ejecucion import EN_CURSO, SIN_CONFIRMAR, estado_en_base

        with self.almacen.transaccion() as db:
            solicitud = self._cargar(db, solicitud_id)
        self._comprobar_ejecutor(usuario, solicitud)
        if solicitud["estado"] != "ejecutando":
            raise Conflicto("Solo se comprueban las solicitudes que se "
                            "quedaron ejecutándose.")
        ejecucion_id = solicitud["ejecucion_id"]
        if not ejecucion_id:
            raise Conflicto("No consta qué ejecución le corresponde: busca "
                            f"la solicitud {solicitud_id} en "
                            "guardian_backup.ejecuciones.")
        conexion = self._conexion(solicitud["conexion"])
        try:
            estado_base, filas = estado_en_base(conexion.dsn, ejecucion_id)
        except psycopg.Error as error:
            raise Conflicto(f"No se pudo comprobar en '{conexion.nombre}': "
                            f"{str(error).strip()}")
        if estado_base == EN_CURSO:
            raise Conflicto("La ejecución sigue en curso en PostgreSQL. "
                            "Vuelve a comprobarlo en unos minutos.")
        if estado_base == SIN_CONFIRMAR:
            estado, motivo = "no_aplicada", (
                "La ejecución se interrumpió antes del COMMIT: PostgreSQL "
                "deshizo la transacción y no se aplicó nada. Reanaliza y "
                "vuelve a aprobar.")
        elif estado_base == "aplicado":
            estado, motivo = "ejecutada", (
                "La ejecución se interrumpió después del COMMIT: los "
                "cambios están aplicados y respaldados. El detalle por "
                "sentencia no llegó a guardarse aquí.")
        else:
            estado, motivo = "deshecha", (
                "La ejecución se interrumpió después del COMMIT y luego se "
                "deshizo fuera de la aplicación.")
        aplicada = estado_base != SIN_CONFIRMAR
        resultado = {"motivo": motivo, "recuperada": True,
                     "aplicada": aplicada, "discrepancias": []}
        if aplicada:
            resultado["filas_respaldadas"] = filas
        with self.almacen.transaccion() as db:
            solicitud = self._cargar(db, solicitud_id)
            self._transicion(db, solicitud, ("ejecutando",), estado=estado,
                             resultado=resultado, ejecutada_en=_ahora(),
                             ejecucion_id=ejecucion_id if aplicada else None)
            self.almacen.registrar(db, usuario, "ejecucion_recuperada",
                                   solicitud_id, {
                                       "ejecucion": ejecucion_id,
                                       "estado": estado,
                                       "filas_respaldadas": filas,
                                       "motivo": motivo})
            return self.almacen.solicitud(solicitud_id, db)

    def recuperar_interrumpidas(self):
        """Intenta cerrar todas las solicitudes en 'ejecutando'.

        Devuelve una línea de texto por solicitud. Se llama al arrancar el
        servidor, cuando ninguna ejecución de este proceso está en marcha.
        """
        lineas = []
        for s in self.almacen.solicitudes("ejecutando"):
            try:
                estado = self.recuperar(SISTEMA, s["id"])["estado"]
                lineas.append(f"Solicitud {s['id']} interrumpida: queda "
                              f"'{estado}'.")
            except ErrorFlujo as error:
                lineas.append(f"Solicitud {s['id']} sigue en 'ejecutando': "
                              f"{error}")
        return lineas

    def deshacer(self, usuario, solicitud_id, comentario=""):
        from guardian.deshacer import ErrorDeshacer, deshacer

        if not _es_aprobador(usuario):
            raise Prohibido("Solo un aprobador puede deshacer una "
                            "ejecución.")
        with self.almacen.transaccion() as db:
            solicitud = self._cargar(db, solicitud_id)
            if solicitud["estado"] != "ejecutada":
                raise Conflicto("Solo se puede deshacer una solicitud "
                                "ejecutada.")
        conexion = self._conexion(solicitud["conexion"])
        try:
            resultado = deshacer(
                conexion.dsn, solicitud["ejecucion_id"], lambda _: True,
                statement_timeout=conexion.statement_timeout,
                lock_timeout=conexion.lock_timeout)
        except (ErrorDeshacer, psycopg.Error) as error:
            with self.almacen.transaccion() as db:
                self.almacen.registrar(db, usuario, "deshacer_fallido",
                                       solicitud_id, {"motivo": str(error)})
            raise Conflicto(f"No se pudo deshacer: {error}")
        detalle = {
            "comentario": (comentario or "").strip(),
            "omitidas": resultado.omitidas,
            "pasos": [{"linea": p.linea, "operacion": p.operacion,
                       "tabla": p.tabla, "respaldadas": p.respaldadas,
                       "restauradas": p.restauradas}
                      for p in resultado.pasos]}
        with self.almacen.transaccion() as db:
            solicitud = self._cargar(db, solicitud_id)
            datos = dict(solicitud["resultado"] or {}, deshacer=detalle)
            self._transicion(db, solicitud, ("ejecutada",),
                             estado="deshecha", resultado=datos)
            self.almacen.registrar(db, usuario, "deshacer", solicitud_id,
                                   detalle)
            return self.almacen.solicitud(solicitud_id, db)


def _resultado_ejecucion(ejecucion):
    return {
        "ejecucion_id": ejecucion.id,
        "aplicada": ejecucion.aplicada,
        "motivo": ejecucion.motivo,
        "filas_respaldadas": ejecucion.filas_respaldadas,
        "bloqueos": [{"linea": b.linea, "mensaje": b.mensaje}
                     for b in ejecucion.bloqueos],
        "pasos": [{"indice": p.indice, "linea": p.linea,
                   "operacion": p.operacion, "tabla": p.tabla,
                   "filas": p.filas, "respaldadas": p.respaldadas,
                   "forzada": p.forzada,
                   "indirectos": {t: list(v) for t, v in p.indirectos.items()},
                   "ignorados": {t: list(v) for t, v in p.ignorados.items()}}
                  for p in ejecucion.pasos],
    }
