"""API HTTP (FastAPI) y archivos estáticos de la aplicación web.

Seguridad:
- Sesión en cookie HttpOnly y SameSite=Strict; el token solo se guarda
  hasheado en SQLite.
- Toda petición que cambia algo debe llevar la cabecera `X-Guardian: 1`.
  Un formulario de otra web no puede añadirla (y no se habilita CORS), lo
  que evita CSRF aunque el navegador no respetara SameSite.
- CSP estricta: la página no usa scripts ni estilos en línea.
- Tras 5 contraseñas fallidas seguidas, el usuario se bloquea 5 minutos.
"""

import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from guardian.web.almacen import ROLES, Almacen
from guardian.web.servicio import ErrorFlujo, Servicio, version

ESTATICO = Path(__file__).resolve().parent / "estatico"
COOKIE = "guardian_sesion"
_INTENTOS_MAXIMOS = 5
_BLOQUEO_SEGUNDOS = 300
_CABECERAS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class Credenciales(BaseModel):
    nombre: str
    clave: str


class NuevaSolicitud(BaseModel):
    titulo: str = Field(max_length=200)
    motivo: str = ""
    conexion: str
    script_nombre: str = "script.sql"
    sql: str
    ignorar_tablas: list[str] = []
    permitir_indirectos: bool = False
    forzar: bool = False


class Comentario(BaseModel):
    comentario: str = ""
    # Al aprobar: la versión de la solicitud que tenía abierta el aprobador.
    version: str | None = None


class NuevoUsuario(BaseModel):
    nombre: str = Field(min_length=2, max_length=60,
                        pattern=r"^[A-Za-z0-9._-]+$")
    nombre_completo: str = ""
    rol: str
    clave: str


class CambiosUsuario(BaseModel):
    """Solo se cambia lo que viene (None = dejarlo como está)."""
    nombre_completo: str | None = Field(default=None, max_length=120)
    rol: str | None = None
    clave: str | None = None


class Activo(BaseModel):
    activo: bool


def crear_app(config):
    almacen = Almacen(config.base_datos)
    servicio = Servicio(config, almacen)
    fallos = {}

    app = FastAPI(title="SQL Guardian", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.almacen = almacen
    app.state.servicio = servicio

    @app.middleware("http")
    async def seguridad(request: Request, siguiente):
        if (request.url.path.startswith("/api/")
                and request.method not in ("GET", "HEAD", "OPTIONS")
                and request.headers.get("x-guardian") != "1"):
            return JSONResponse({"detail": "Falta la cabecera X-Guardian."},
                                status_code=403)
        respuesta = await siguiente(request)
        for nombre, valor in _CABECERAS.items():
            respuesta.headers.setdefault(nombre, valor)
        if request.url.path.startswith("/api/"):
            respuesta.headers["Cache-Control"] = "no-store"
        return respuesta

    @app.exception_handler(ErrorFlujo)
    async def error_flujo(_request, error: ErrorFlujo):
        return JSONResponse({"detail": str(error)},
                            status_code=error.estado_http)

    def usuario_actual(request: Request):
        usuario = almacen.usuario_de_sesion(request.cookies.get(COOKIE))
        if usuario is None:
            raise HTTPException(401, "Inicia sesión.")
        return usuario

    def solo_admin(usuario=Depends(usuario_actual)):
        if usuario["rol"] != "admin":
            raise HTTPException(403, "Solo un administrador puede hacerlo.")
        return usuario

    # --- Sesión ---------------------------------------------------------

    @app.post("/api/sesion")
    def entrar(datos: Credenciales, request: Request, response: Response):
        nombre = datos.nombre.strip()
        intentos, desde = fallos.get(nombre, (0, 0.0))
        if (intentos >= _INTENTOS_MAXIMOS
                and time.monotonic() - desde < _BLOQUEO_SEGUNDOS):
            raise HTTPException(429, "Demasiados intentos. Espera unos "
                                "minutos.")
        resultado = almacen.iniciar_sesion(nombre, datos.clave,
                                           config.minutos_sesion)
        if resultado is None:
            fallos[nombre] = (intentos + 1, time.monotonic())
            with almacen.transaccion() as db:
                almacen.registrar(db, nombre or "?", "acceso_fallido")
            raise HTTPException(401, "Usuario o contraseña incorrectos.")
        fallos.pop(nombre, None)
        token, usuario = resultado
        response.set_cookie(
            COOKIE, token, httponly=True, samesite="strict",
            secure=request.url.scheme == "https",
            max_age=config.minutos_sesion * 60, path="/")
        with almacen.transaccion() as db:
            almacen.registrar(db, usuario, "acceso")
        return usuario

    @app.delete("/api/sesion")
    def salir(request: Request, response: Response):
        almacen.cerrar_sesion(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/yo")
    def yo(usuario=Depends(usuario_actual)):
        return usuario

    # --- Solicitudes ----------------------------------------------------

    @app.get("/api/conexiones")
    def conexiones(_usuario=Depends(usuario_actual)):
        return [c.publica() for c in config.conexiones.values()]

    @app.get("/api/solicitudes")
    def listar(estado: str | None = None,
               _usuario=Depends(usuario_actual)):
        return almacen.solicitudes(estado)

    def completa(solicitud):
        """La solicitud con su historial y su versión, para el detalle."""
        solicitud["eventos"] = almacen.eventos(solicitud["id"])
        solicitud["version"] = version(solicitud)
        return solicitud

    @app.post("/api/solicitudes", status_code=201)
    def crear(datos: NuevaSolicitud, usuario=Depends(usuario_actual)):
        return servicio.crear(usuario, **datos.model_dump())

    @app.get("/api/solicitudes/{solicitud_id}")
    def detalle(solicitud_id: int, _usuario=Depends(usuario_actual)):
        solicitud = almacen.solicitud(solicitud_id)
        if solicitud is None:
            raise HTTPException(404, "No existe la solicitud.")
        return completa(solicitud)

    @app.patch("/api/solicitudes/{solicitud_id}")
    def editar(solicitud_id: int, datos: NuevaSolicitud,
               usuario=Depends(usuario_actual)):
        return completa(servicio.editar(usuario, solicitud_id,
                                        **datos.model_dump()))

    def _accion(nombre):
        def accion(solicitud_id: int, datos: Comentario | None = None,
                   usuario=Depends(usuario_actual)):
            comentario = datos.comentario if datos else ""
            metodo = getattr(servicio, nombre)
            if nombre in ("reanalizar", "ejecutar", "recuperar"):
                solicitud = metodo(usuario, solicitud_id)
            elif nombre == "aprobar":
                solicitud = metodo(usuario, solicitud_id, comentario,
                                   vista=datos.version if datos else None)
            else:
                solicitud = metodo(usuario, solicitud_id, comentario)
            return completa(solicitud)
        accion.__name__ = nombre
        return accion

    for nombre in ("aprobar", "rechazar", "cancelar", "comentar",
                   "reanalizar", "ejecutar", "recuperar", "deshacer"):
        app.post(f"/api/solicitudes/{{solicitud_id}}/{nombre}")(
            _accion(nombre))

    # --- Usuarios y auditoría -------------------------------------------

    @app.get("/api/usuarios")
    def usuarios(_admin=Depends(solo_admin)):
        return almacen.usuarios()

    @app.post("/api/usuarios", status_code=201)
    def crear_usuario(datos: NuevoUsuario, admin=Depends(solo_admin)):
        if datos.rol not in ROLES:
            raise HTTPException(400, f"Rol no válido: {datos.rol}")
        try:
            usuario_id = almacen.crear_usuario(
                datos.nombre, datos.clave, datos.rol, datos.nombre_completo)
        except ValueError as error:
            raise HTTPException(400, str(error))
        with almacen.transaccion() as db:
            almacen.registrar(db, admin, "crear_usuario", None,
                              {"usuario": datos.nombre, "rol": datos.rol})
        return almacen.usuario(usuario_id)

    @app.patch("/api/usuarios/{usuario_id}")
    def modificar_usuario(usuario_id: int, datos: CambiosUsuario,
                          request: Request, admin=Depends(solo_admin)):
        objetivo = almacen.usuario(usuario_id)
        if objetivo is None:
            raise HTTPException(404, "No existe el usuario.")
        yo_mismo = objetivo["id"] == admin["id"]
        if yo_mismo and datos.rol not in (None, "admin"):
            raise HTTPException(400, "No puedes quitarte a ti mismo el rol "
                                "de administrador.")
        nombre_completo = (datos.nombre_completo.strip()
                           if datos.nombre_completo is not None else None)
        try:
            almacen.modificar_usuario(
                usuario_id, admin, nombre_completo=nombre_completo,
                rol=datos.rol, clave=datos.clave,
                conservar_token=(request.cookies.get(COOKIE)
                                 if yo_mismo else None))
        except ValueError as error:
            raise HTTPException(400, str(error))
        return almacen.usuario(usuario_id)

    @app.post("/api/usuarios/{usuario_id}/activo")
    def activar(usuario_id: int, datos: Activo, admin=Depends(solo_admin)):
        objetivo = almacen.usuario(usuario_id)
        if objetivo is None:
            raise HTTPException(404, "No existe el usuario.")
        if objetivo["id"] == admin["id"] and not datos.activo:
            raise HTTPException(400, "No puedes desactivarte a ti mismo.")
        almacen.cambiar_activo(usuario_id, datos.activo)
        with almacen.transaccion() as db:
            almacen.registrar(db, admin, "activar_usuario" if datos.activo
                              else "desactivar_usuario", None,
                              {"usuario": objetivo["nombre"]})
        return almacen.usuario(usuario_id)

    @app.get("/api/auditoria")
    def auditoria(limite: int = 200, _usuario=Depends(usuario_actual)):
        intacta, dato = almacen.verificar_auditoria()
        return {"intacta": intacta,
                "eventos_verificados": dato if intacta else None,
                "primer_evento_alterado": None if intacta else dato,
                "eventos": almacen.eventos(limite=min(limite, 1000))}

    # --- Página ---------------------------------------------------------

    app.mount("/estatico", StaticFiles(directory=ESTATICO), name="estatico")

    @app.get("/")
    def inicio():
        return FileResponse(ESTATICO / "index.html")

    return app
