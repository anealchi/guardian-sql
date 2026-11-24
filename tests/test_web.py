"""Aplicación web: sesión, flujo de aprobación, ejecución y auditoría."""

import sqlite3

import psycopg
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from guardian.respaldo import clave_bloqueo, nuevo_id  # noqa: E402
from guardian.web import almacen as modulo_almacen  # noqa: E402
from guardian.web.app import crear_app  # noqa: E402
from guardian.web.config import Config, Conexion  # noqa: E402

CLAVE = "clave-de-prueba-123"
CABECERA = {"X-Guardian": "1"}


@pytest.fixture(autouse=True)
def hash_rapido(monkeypatch):
    # PBKDF2 con 600 000 iteraciones haría las pruebas muy lentas.
    monkeypatch.setattr(modulo_almacen, "ITERACIONES", 1000)
    monkeypatch.setattr(modulo_almacen, "_CLAVE_FICTICIA", [])


def _config(tmp_path, dsn, simular=True):
    return Config(
        base_datos=tmp_path / "web.db",
        conexiones={
            "pruebas": Conexion("pruebas", dsn, simular=simular,
                                ignorar_tablas=["parte_trabajo_tarea_log"]),
            "caida": Conexion(
                "caida", "postgresql://nadie@127.0.0.1:1/x?connect_timeout=2"),
        })


@pytest.fixture
def app(tmp_path, dsn):
    aplicacion = crear_app(_config(tmp_path, dsn))
    almacen = aplicacion.state.almacen
    almacen.crear_usuario("ana", CLAVE, "solicitante", "Ana")
    almacen.crear_usuario("luis", CLAVE, "aprobador", "Luis")
    almacen.crear_usuario("admin", CLAVE, "admin", "Admin")
    return aplicacion


def cliente(app, nombre=None):
    c = TestClient(app)
    if nombre:
        r = c.post("/api/sesion", json={"nombre": nombre, "clave": CLAVE},
                   headers=CABECERA)
        assert r.status_code == 200, r.text
    return c


def post(c, ruta, **json):
    return c.post(ruta, json=json or None, headers=CABECERA)


def nueva(c, script_real, nombre="borrar_tareas_duplicadas_parte.sql",
          conexion="pruebas", **extra):
    r = post(c, "/api/solicitudes", titulo="Borrar duplicados",
             motivo="Prueba", conexion=conexion, script_nombre=nombre,
             sql=script_real(nombre), **extra)
    assert r.status_code == 201, r.text
    return r.json()


def borradas(dsn):
    with psycopg.connect(dsn) as conexion:
        return conexion.execute("select count(*) from parte_trabajo_tarea "
                                "where estado is false").fetchone()[0]


def test_sesion(app):
    c = cliente(app)
    assert c.get("/api/yo").status_code == 401
    r = post(c, "/api/sesion", nombre="ana", clave="mala")
    assert r.status_code == 401
    r = post(c, "/api/sesion", nombre="ana", clave=CLAVE)
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert c.get("/api/yo").json()["nombre"] == "ana"
    c.delete("/api/sesion", headers=CABECERA)
    assert c.get("/api/yo").status_code == 401


def test_bloqueo_tras_varios_fallos(app):
    c = cliente(app)
    for _ in range(5):
        post(c, "/api/sesion", nombre="ana", clave="mala")
    r = post(c, "/api/sesion", nombre="ana", clave=CLAVE)
    assert r.status_code == 429


def test_peticiones_sin_cabecera_se_rechazan(app):
    c = cliente(app, "ana")
    r = c.post("/api/solicitudes", json={})
    assert r.status_code == 403
    assert "X-Guardian" in r.json()["detail"]


def test_cabeceras_de_seguridad_y_pagina(app):
    r = cliente(app).get("/")
    assert r.status_code == 200
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_conexiones_no_exponen_el_dsn(app):
    datos = cliente(app, "ana").get("/api/conexiones").json()
    assert {c["nombre"] for c in datos} == {"pruebas", "caida"}
    assert all("dsn" not in c for c in datos)


def test_flujo_completo(app, dsn, script_real):
    ana, luis = cliente(app, "ana"), cliente(app, "luis")
    solicitud = nueva(ana, script_real)
    assert solicitud["estado"] == "pendiente"
    assert solicitud["informe"]["impacto"]["modo"] == "simulacion"
    assert solicitud["informe"]["filas_previstas"] == 6
    assert solicitud["opciones"]["ignorar_tablas"] == [
        "parte_trabajo_tarea_log"]
    sid = solicitud["id"]

    # Nadie aprueba lo suyo, y un solicitante no aprueba.
    assert post(ana, f"/api/solicitudes/{sid}/aprobar").status_code == 403
    # Ejecutar sin aprobar: conflicto.
    assert post(ana, f"/api/solicitudes/{sid}/ejecutar").status_code == 409

    r = post(luis, f"/api/solicitudes/{sid}/aprobar", comentario="OK")
    assert r.json()["estado"] == "aprobada"
    assert r.json()["aprobador"] == "luis"

    r = post(ana, f"/api/solicitudes/{sid}/ejecutar")
    datos = r.json()
    assert datos["estado"] == "ejecutada", datos["resultado"]
    assert datos["resultado"]["filas_respaldadas"] == 6
    assert borradas(dsn) == 2
    with psycopg.connect(dsn) as conexion:
        origen = conexion.execute(
            "select solicitud, solicitado_por, aprobado_por from "
            "guardian_backup.ejecuciones where id = %s",
            (datos["ejecucion_id"],)).fetchone()
    assert origen == (str(sid), "ana", "luis")

    # Deshacer: solo un aprobador.
    assert post(ana, f"/api/solicitudes/{sid}/deshacer").status_code == 403
    r = post(luis, f"/api/solicitudes/{sid}/deshacer", comentario="Error")
    assert r.json()["estado"] == "deshecha"
    assert borradas(dsn) == 0

    acciones = [e["accion"] for e in r.json()["eventos"]]
    assert acciones == ["crear", "aprobar", "ejecucion_iniciada",
                        "ejecucion_ejecutada", "deshacer"]
    # Ningún evento del flujo (p. ej. filas_aprobadas, con claves enteras)
    # debe romper la cadena de hashes.
    assert ana.get("/api/auditoria").json()["intacta"]


def test_si_cambian_los_datos_no_se_aplica(app, dsn, script_real):
    ana, luis = cliente(app, "ana"), cliente(app, "luis")
    sid = nueva(ana, script_real)["id"]
    post(luis, f"/api/solicitudes/{sid}/aprobar")
    # Otra tarea duplicada aparece después de aprobar.
    with psycopg.connect(dsn, autocommit=True) as conexion:
        conexion.execute(
            "insert into tarea_planificacion (id, codigo, "
            "tarea_plan_mantenimiento_id, fecha_proxima_ejecucion, "
            "empresa_ubicacion_id, empresa_activo_id) values "
            "(7, 'TP-7', 12, '2026-11-23 08:00', 100, 501)")
        conexion.execute("insert into orden_trabajo_tarea (id, "
                         "orden_trabajo_id, tarea_planificacion_id) "
                         "values (7, 1, 7)")
        conexion.execute("insert into parte_trabajo_tarea (id, "
                         "parte_trabajo_id, orden_trabajo_tarea_id) "
                         "values (7, 1, 7)")
    datos = post(ana, f"/api/solicitudes/{sid}/ejecutar").json()
    assert datos["estado"] == "no_aplicada"
    assert datos["resultado"]["discrepancias"][0]["aprobadas"] == 2
    assert datos["resultado"]["discrepancias"][0]["reales"] == 3
    assert borradas(dsn) == 0

    # Reanalizar la devuelve a pendiente y anula la aprobación.
    datos = post(ana, f"/api/solicitudes/{sid}/reanalizar").json()
    assert datos["estado"] == "pendiente" and datos["aprobador"] is None
    assert datos["informe"]["filas_previstas"] == 9


def _forzar(app, sid, **campos):
    """Cambia la solicitud en SQLite como la dejaría un servidor caído."""
    db = sqlite3.connect(app.state.almacen.ruta)
    asignaciones = ", ".join(f"{c} = ?" for c in campos)
    db.execute(f"update solicitudes set {asignaciones} where id = ?",
               [*campos.values(), sid])
    db.commit()
    db.close()


def _aprobada(app, script_real):
    ana, luis = cliente(app, "ana"), cliente(app, "luis")
    sid = nueva(ana, script_real)["id"]
    assert post(luis, f"/api/solicitudes/{sid}/aprobar").status_code == 200
    return ana, luis, sid


def test_ejecucion_interrumpida_antes_del_commit(app, dsn, script_real):
    ana, _luis, sid = _aprobada(app, script_real)
    # El servidor murió con la transacción abierta: PostgreSQL la deshizo
    # y no quedó nada en guardian_backup.
    _forzar(app, sid, estado="ejecutando", ejecucion_id=nuevo_id())
    datos = post(ana, f"/api/solicitudes/{sid}/recuperar").json()
    assert datos["estado"] == "no_aplicada"
    assert datos["ejecucion_id"] is None
    assert "antes del COMMIT" in datos["resultado"]["motivo"]
    assert datos["eventos"][-1]["accion"] == "ejecucion_recuperada"
    assert borradas(dsn) == 0
    # Como cualquier no aplicada: se reanaliza y se vuelve a aprobar.
    r = post(ana, f"/api/solicitudes/{sid}/reanalizar")
    assert r.json()["estado"] == "pendiente"


def test_ejecucion_interrumpida_despues_del_commit(app, dsn, script_real):
    ana, luis, sid = _aprobada(app, script_real)
    ejecucion = post(ana, f"/api/solicitudes/{sid}/ejecutar").json()[
        "ejecucion_id"]
    # Murió tras el COMMIT, antes de anotar el resultado en SQLite.
    _forzar(app, sid, estado="ejecutando", resultado=None)
    datos = post(luis, f"/api/solicitudes/{sid}/recuperar").json()
    assert datos["estado"] == "ejecutada"
    assert datos["ejecucion_id"] == ejecucion
    assert datos["resultado"]["filas_respaldadas"] == 6
    assert borradas(dsn) == 2
    r = post(luis, f"/api/solicitudes/{sid}/deshacer", comentario="Error")
    assert r.json()["estado"] == "deshecha"
    assert borradas(dsn) == 0


def test_ejecucion_en_curso_no_se_toca(app, dsn, script_real):
    ana, _luis, sid = _aprobada(app, script_real)
    assert post(ana, f"/api/solicitudes/{sid}/recuperar").status_code == 409
    ejecucion = nuevo_id()
    _forzar(app, sid, estado="ejecutando", ejecucion_id=ejecucion)
    servicio = app.state.servicio
    # Otra conexión mantiene el bloqueo, como la de una ejecución viva.
    with psycopg.connect(dsn, autocommit=True) as viva:
        viva.execute("select pg_advisory_lock(%s)",
                     (clave_bloqueo(ejecucion),))
        r = post(ana, f"/api/solicitudes/{sid}/recuperar")
        assert r.status_code == 409 and "en curso" in r.json()["detail"]
        assert "sigue en 'ejecutando'" in servicio.recuperar_interrumpidas()[0]
    # Al cerrarse esa conexión, el arranque del servidor la cierra.
    assert servicio.recuperar_interrumpidas() == [
        f"Solicitud {sid} interrumpida: queda 'no_aplicada'."]
    eventos = ana.get(f"/api/solicitudes/{sid}").json()["eventos"]
    assert eventos[-1]["usuario"] == "servidor"


def test_si_no_conecta_al_ejecutar_sigue_aprobada(app, script_real):
    ana, _luis, sid = _aprobada(app, script_real)
    _forzar(app, sid, conexion="caida")
    r = post(ana, f"/api/solicitudes/{sid}/ejecutar")
    assert r.status_code == 400
    assert "No se pudo empezar" in r.json()["detail"]
    datos = ana.get(f"/api/solicitudes/{sid}").json()
    assert datos["estado"] == "aprobada"
    assert "ejecucion_iniciada" not in [e["accion"] for e in datos["eventos"]]


def test_rechazar_y_cancelar(app, script_real):
    ana, luis = cliente(app, "ana"), cliente(app, "luis")
    sid = nueva(ana, script_real)["id"]
    assert post(luis, f"/api/solicitudes/{sid}/rechazar").status_code == 400
    r = post(luis, f"/api/solicitudes/{sid}/rechazar", comentario="No")
    assert r.json()["estado"] == "rechazada"
    assert post(ana, f"/api/solicitudes/{sid}/cancelar").status_code == 409

    sid = nueva(ana, script_real)["id"]
    assert post(luis, f"/api/solicitudes/{sid}/cancelar").status_code == 403
    assert post(ana, f"/api/solicitudes/{sid}/cancelar").json()[
        "estado"] == "cancelada"


def test_sin_impacto_no_se_puede_aprobar(app, script_real):
    ana, luis = cliente(app, "ana"), cliente(app, "luis")
    solicitud = nueva(ana, script_real, conexion="caida")
    assert solicitud["informe"]["impacto"] is None
    assert "No se pudo calcular" in solicitud["informe"]["error"]
    r = post(luis, f"/api/solicitudes/{solicitud['id']}/aprobar")
    assert r.status_code == 409


def test_errores_de_entrada(app):
    ana = cliente(app, "ana")
    r = post(ana, "/api/solicitudes", titulo="x", conexion="pruebas",
             sql="updat t set a = 1")
    assert r.status_code == 400 and "sintaxis" in r.json()["detail"]
    r = post(ana, "/api/solicitudes", titulo="x", conexion="otra",
             sql="select 1")
    assert r.status_code == 400
    assert ana.get("/api/solicitudes/999").status_code == 404


def test_usuarios_solo_admin(app):
    assert cliente(app, "luis").get("/api/usuarios").status_code == 403
    admin = cliente(app, "admin")
    r = post(admin, "/api/usuarios", nombre="eva", rol="aprobador",
             clave=CLAVE, nombre_completo="Eva")
    assert r.status_code == 201
    eva_id = r.json()["id"]
    assert post(admin, "/api/usuarios", nombre="eva", rol="aprobador",
                clave=CLAVE).status_code == 400
    assert post(admin, "/api/usuarios", nombre="corta", rol="admin",
                clave="123").status_code == 400
    eva = cliente(app, "eva")
    post(admin, f"/api/usuarios/{eva_id}/activo", activo=False)
    assert eva.get("/api/yo").status_code == 401


def test_detalle_con_claves_enteras_no_rompe_la_cadena(app):
    # filas_aprobadas usa el índice de sentencia como clave: con índices de
    # dos cifras, el orden de las claves cambia al pasar por JSON.
    almacen = app.state.almacen
    with almacen.transaccion() as db:
        almacen.registrar(db, "luis", "aprobar", None,
                          {"filas_aprobadas": {9: 1, 10: 2, 11: 0}})
    assert almacen.verificar_auditoria() == (True, 1)


def test_cadena_de_auditoria(app, tmp_path, script_real):
    ana = cliente(app, "ana")
    nueva(ana, script_real)
    datos = ana.get("/api/auditoria").json()
    assert datos["intacta"] and datos["eventos_verificados"] >= 2

    ruta = tmp_path / "web.db"
    db = sqlite3.connect(ruta)
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("update eventos set usuario = 'otro' where id = 1")
    # Alguien con acceso al archivo quita el trigger y manipula un evento.
    db.execute("drop trigger eventos_sin_cambios")
    db.execute("update eventos set usuario = 'otro' where id = 1")
    db.commit()
    db.close()
    datos = ana.get("/api/auditoria").json()
    assert not datos["intacta"] and datos["primer_evento_alterado"] == 1
