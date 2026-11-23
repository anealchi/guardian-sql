"""Reglas del flujo de solicitudes, independientes de HTTP.

    pendiente ──aprobar──▶ aprobada ──ejecutar──▶ ejecutando ─┬─▶ ejecutada
        │  ▲                  │                               ├─▶ no_aplicada
        │  └──reanalizar──────┤                               └─▶ fallida
        ├──rechazar──▶ rechazada              ejecutada ──deshacer──▶ deshecha
        └──cancelar──▶ cancelada

Reglas principales:

- Quien aprueba es un aprobador o admin **distinto** de quien solicita.
- Solo se aprueba con el impacto calculado contra la base de datos, y lo
  que se aprueba es exactamente ese script (se comprueba su hash) con esas
  opciones.
- Al ejecutar, si una sentencia cambia un número de filas distinto del que
  vio el aprobador (en los recuentos exactos), no se hace COMMIT: hay que
  reanalizar y volver a aprobar.
- Todo cambio de estado deja un evento en la cadena de auditoría, en la
  misma transacción de SQLite.
"""

import hashlib

import psycopg

from guardian.analisis import NIVELES, analizar
from guardian.informe import informe_json
from guardian.sentencias import ErrorSintaxis, leer_script
from guardian.web.almacen import ahora as _ahora

TAMANO_MAXIMO = 1_000_000
_MODIFICACIONES = ("UPDATE", "DELETE", "INSERT")


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

    def crear(self, usuario, titulo, motivo, conexion, script_nombre, sql,
              ignorar_tablas=(), permitir_indirectos=False, forzar=False):
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
        opciones = {"ignorar_tablas": ignorar,
                    "permitir_indirectos": bool(permitir_indirectos),
                    "forzar": bool(forzar)}
        informe = self.analizar(conexion, sql)
        with self.almacen.transaccion() as db:
            solicitud_id = self.almacen.insertar_solicitud(db, {
                "titulo": titulo[:200], "motivo": (motivo or "").strip(),
                "conexion": conexion,
                "script_nombre": (script_nombre or "script.sql")[:200],
                "sql": sql, "hash": _hash(sql), "opciones": opciones,
                "estado": "pendiente", "informe": informe,
                "informe_fecha": _ahora(), "solicitante_id": usuario["id"]})
            self.almacen.registrar(db, usuario, "crear", solicitud_id, {
                "hash": _hash(sql), "conexion": conexion,
                "opciones": opciones,
                "veredicto": informe["veredicto"]["nivel"],
                "filas_previstas": informe["filas_previstas"]})
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

    def aprobar(self, usuario, solicitud_id, comentario=""):
        if not _es_aprobador(usuario):
            raise Prohibido("Solo un aprobador puede aprobar.")
        with self.almacen.transaccion() as db:
            solicitud = self._cargar(db, solicitud_id)
            if solicitud["solicitante_id"] == usuario["id"]:
                raise Prohibido("No puedes aprobar tu propia solicitud: "
                                "tiene que hacerlo otra persona.")
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
            self.almacen.registrar(db, usuario, "aprobar", solicitud_id, {
                "hash": solicitud["hash"], "comentario": comentario.strip(),
                "filas_aprobadas": filas_aprobadas(informe)})
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

    def ejecutar(self, usuario, solicitud_id):
        from guardian.ejecucion import ErrorEjecucion, ejecutar

        with self.almacen.transaccion() as db:
            solicitud = self._cargar(db, solicitud_id)
            if (usuario["id"] != solicitud["solicitante_id"]
                    and not _es_aprobador(usuario)):
                raise Prohibido("Solo quien la pidió o un aprobador puede "
                                "ejecutarla.")
            if _hash(solicitud["sql"]) != solicitud["hash"]:
                raise Conflicto("El script no coincide con su hash.")
            self._transicion(db, solicitud, ("aprobada",),
                             estado="ejecutando", ejecutor_id=usuario["id"])
            self.almacen.registrar(db, usuario, "ejecucion_iniciada",
                                   solicitud_id, {"hash": solicitud["hash"]})

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
        ejecucion_id = None
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
                        "aprobado_por": solicitud["aprobador"]})
            resultado.update(_resultado_ejecucion(ejecucion))
            if ejecucion.aplicada:
                estado, ejecucion_id = "ejecutada", ejecucion.id
            else:
                estado = "no_aplicada"
                if discrepancias:
                    resultado["motivo"] = (
                        "El número de filas no coincide con el aprobado: "
                        "se ha hecho ROLLBACK. Reanaliza y vuelve a "
                        "aprobar.")
        except ErrorEjecucion as error:
            resultado["motivo"] = f"Error, se ha hecho ROLLBACK. {error}"
        except Exception as error:  # noqa: BLE001 - se registra y se informa
            resultado["motivo"] = f"Error inesperado: {error}"
        finally:
            with self.almacen.transaccion() as db:
                self.almacen.actualizar_solicitud(
                    db, solicitud_id, estado=estado, resultado=resultado,
                    ejecucion_id=ejecucion_id, ejecutada_en=_ahora())
                self.almacen.registrar(db, usuario, f"ejecucion_{estado}",
                                       solicitud_id, {
                                           "ejecucion": ejecucion_id,
                                           "filas_respaldadas": resultado.get(
                                               "filas_respaldadas"),
                                           "motivo": resultado.get("motivo"),
                                           "discrepancias": discrepancias})
        return self.almacen.solicitud(solicitud_id)

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
