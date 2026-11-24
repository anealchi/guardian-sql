"""Datos de la aplicación web en SQLite.

- `usuarios` y `sesiones`: acceso con contraseña (PBKDF2) y cookie.
- `solicitudes`: cada script enviado, con su informe y su estado.
- `eventos`: registro de auditoría de solo inserción. Cada evento guarda el
  hash del anterior, así que cambiar o borrar uno rompe la cadena y
  `verificar_auditoria()` lo detecta.
"""

import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

ROLES = ("solicitante", "aprobador", "admin")
ITERACIONES = 600_000

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY,
    nombre TEXT NOT NULL UNIQUE,
    nombre_completo TEXT NOT NULL DEFAULT '',
    rol TEXT NOT NULL CHECK (rol IN ('solicitante', 'aprobador', 'admin')),
    clave TEXT NOT NULL,
    activo INTEGER NOT NULL DEFAULT 1,
    creado TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sesiones (
    token TEXT PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios,
    expira TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS solicitudes (
    id INTEGER PRIMARY KEY,
    titulo TEXT NOT NULL,
    motivo TEXT NOT NULL DEFAULT '',
    conexion TEXT NOT NULL,
    script_nombre TEXT NOT NULL,
    sql TEXT NOT NULL,
    hash TEXT NOT NULL,
    opciones TEXT NOT NULL DEFAULT '{}',
    estado TEXT NOT NULL,
    informe TEXT,
    informe_fecha TEXT,
    solicitante_id INTEGER NOT NULL REFERENCES usuarios,
    creada TEXT NOT NULL,
    aprobador_id INTEGER REFERENCES usuarios,
    aprobada_en TEXT,
    ejecutor_id INTEGER REFERENCES usuarios,
    ejecutada_en TEXT,
    ejecucion_id TEXT,
    resultado TEXT,
    actualizada TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS solicitudes_estado ON solicitudes (estado);

CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY,
    fecha TEXT NOT NULL,
    usuario TEXT NOT NULL,
    accion TEXT NOT NULL,
    solicitud_id INTEGER,
    detalle TEXT NOT NULL DEFAULT '{}',
    hash_anterior TEXT NOT NULL,
    hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS eventos_solicitud ON eventos (solicitud_id);

CREATE TRIGGER IF NOT EXISTS eventos_sin_cambios
BEFORE UPDATE ON eventos
BEGIN SELECT RAISE(ABORT, 'el registro de auditoría no se modifica'); END;

CREATE TRIGGER IF NOT EXISTS eventos_sin_borrados
BEFORE DELETE ON eventos
BEGIN SELECT RAISE(ABORT, 'el registro de auditoría no se borra'); END;
"""

_JSON = ("opciones", "informe", "resultado")


def ahora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Contraseñas --------------------------------------------------------

def cifrar_clave(clave, iteraciones=None):
    iteraciones = iteraciones or ITERACIONES
    sal = secrets.token_bytes(16)
    resumen = hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), sal,
                                  iteraciones)
    return f"pbkdf2_sha256${iteraciones}${sal.hex()}${resumen.hex()}"


def comprobar_clave(clave, cifrada):
    try:
        _algoritmo, iteraciones, sal, resumen = cifrada.split("$")
        calculado = hashlib.pbkdf2_hmac(
            "sha256", clave.encode("utf-8"), bytes.fromhex(sal),
            int(iteraciones))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(calculado.hex(), resumen)


_CLAVE_FICTICIA = []


def _clave_ficticia():
    """Hash con el mismo coste que uno real, para usuarios inexistentes."""
    if not _CLAVE_FICTICIA:
        _CLAVE_FICTICIA.append(cifrar_clave(secrets.token_hex(8)))
    return _CLAVE_FICTICIA[0]


def _hash_token(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _hash_evento(anterior, fecha, usuario, accion, solicitud_id, detalle):
    contenido = json.dumps([anterior, fecha, usuario, accion, solicitud_id,
                            detalle], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(contenido.encode("utf-8")).hexdigest()


class Almacen:
    def __init__(self, ruta):
        self.ruta = str(ruta)
        # executescript confirma por su cuenta: va fuera de transaccion().
        db = sqlite3.connect(self.ruta, timeout=30)
        try:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(_ESQUEMA)
        finally:
            db.close()

    @contextmanager
    def transaccion(self):
        """Conexión con BEGIN IMMEDIATE: escrituras serializadas."""
        db = sqlite3.connect(self.ruta, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    # --- Usuarios -------------------------------------------------------

    def crear_usuario(self, nombre, clave, rol, nombre_completo=""):
        if rol not in ROLES:
            raise ValueError(f"Rol no válido: {rol}")
        if len(clave) < 10:
            raise ValueError("La contraseña debe tener al menos 10 "
                             "caracteres.")
        with self.transaccion() as db:
            try:
                cur = db.execute(
                    "INSERT INTO usuarios (nombre, nombre_completo, rol, "
                    "clave, creado) VALUES (?, ?, ?, ?, ?)",
                    (nombre, nombre_completo, rol, cifrar_clave(clave),
                     ahora()))
            except sqlite3.IntegrityError:
                raise ValueError(f"Ya existe el usuario {nombre}.")
            return cur.lastrowid

    def usuarios(self):
        with self.transaccion() as db:
            return [dict(f) for f in db.execute(
                "SELECT id, nombre, nombre_completo, rol, activo, creado "
                "FROM usuarios ORDER BY nombre")]

    def usuario(self, usuario_id):
        with self.transaccion() as db:
            fila = db.execute(
                "SELECT id, nombre, nombre_completo, rol, activo "
                "FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
        return dict(fila) if fila else None

    def cambiar_activo(self, usuario_id, activo):
        with self.transaccion() as db:
            db.execute("UPDATE usuarios SET activo = ? WHERE id = ?",
                       (1 if activo else 0, usuario_id))
            if not activo:
                db.execute("DELETE FROM sesiones WHERE usuario_id = ?",
                           (usuario_id,))

    def hay_usuarios(self):
        with self.transaccion() as db:
            total = db.execute("SELECT count(*) FROM usuarios").fetchone()
        return total[0] > 0

    # --- Sesiones -------------------------------------------------------

    def iniciar_sesion(self, nombre, clave, minutos):
        """Devuelve (token, usuario) o None si los datos no son válidos."""
        with self.transaccion() as db:
            fila = db.execute(
                "SELECT id, nombre, nombre_completo, rol, activo, clave "
                "FROM usuarios WHERE nombre = ?", (nombre,)).fetchone()
        # Se calcula el hash aunque el usuario no exista, para no revelar
        # por el tiempo de respuesta qué usuarios hay.
        cifrada = fila["clave"] if fila else _clave_ficticia()
        valida = comprobar_clave(clave, cifrada)
        if not fila or not valida or not fila["activo"]:
            return None
        token = secrets.token_urlsafe(32)
        expira = (datetime.now(timezone.utc)
                  + timedelta(minutes=minutos)).isoformat(timespec="seconds")
        with self.transaccion() as db:
            db.execute("DELETE FROM sesiones WHERE expira < ?", (ahora(),))
            db.execute("INSERT INTO sesiones (token, usuario_id, expira) "
                       "VALUES (?, ?, ?)", (_hash_token(token), fila["id"],
                                            expira))
        usuario = {k: fila[k] for k in ("id", "nombre", "nombre_completo",
                                        "rol")}
        return token, usuario

    def usuario_de_sesion(self, token):
        if not token:
            return None
        with self.transaccion() as db:
            fila = db.execute(
                "SELECT u.id, u.nombre, u.nombre_completo, u.rol "
                "FROM sesiones s JOIN usuarios u ON u.id = s.usuario_id "
                "WHERE s.token = ? AND s.expira > ? AND u.activo = 1",
                (_hash_token(token), ahora())).fetchone()
        return dict(fila) if fila else None

    def cerrar_sesion(self, token):
        with self.transaccion() as db:
            db.execute("DELETE FROM sesiones WHERE token = ?",
                       (_hash_token(token or ""),))

    # --- Auditoría ------------------------------------------------------

    def registrar(self, db, usuario, accion, solicitud_id=None,
                  detalle=None):
        """Añade un evento a la cadena (dentro de la transacción `db`)."""
        # El hash se calcula sobre el detalle tal y como se leerá después.
        # JSON convierte las claves enteras en texto y sort_keys las ordena
        # entonces de otra forma ({9, 10} frente a {"10", "9"}): sin esto,
        # la verificación lo tomaría por una manipulación.
        detalle = json.loads(json.dumps(detalle or {}, ensure_ascii=False))
        anterior = db.execute(
            "SELECT hash FROM eventos ORDER BY id DESC LIMIT 1").fetchone()
        anterior = anterior[0] if anterior else ""
        fecha = ahora()
        nombre = usuario["nombre"] if isinstance(usuario, dict) else usuario
        resumen = _hash_evento(anterior, fecha, nombre, accion, solicitud_id,
                               detalle)
        db.execute(
            "INSERT INTO eventos (fecha, usuario, accion, solicitud_id, "
            "detalle, hash_anterior, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fecha, nombre, accion, solicitud_id,
             json.dumps(detalle, ensure_ascii=False, sort_keys=True),
             anterior, resumen))

    def eventos(self, solicitud_id=None, limite=200):
        with self.transaccion() as db:
            if solicitud_id is None:
                filas = db.execute(
                    "SELECT * FROM eventos ORDER BY id DESC LIMIT ?",
                    (limite,)).fetchall()
            else:
                filas = db.execute(
                    "SELECT * FROM eventos WHERE solicitud_id = ? "
                    "ORDER BY id", (solicitud_id,)).fetchall()
        return [dict(f, detalle=json.loads(f["detalle"])) for f in filas]

    def verificar_auditoria(self):
        """(True, n) si la cadena está intacta; (False, id) si no."""
        anterior = ""
        total = 0
        with self.transaccion() as db:
            for f in db.execute("SELECT * FROM eventos ORDER BY id"):
                esperado = _hash_evento(
                    anterior, f["fecha"], f["usuario"], f["accion"],
                    f["solicitud_id"], json.loads(f["detalle"]))
                if f["hash_anterior"] != anterior or f["hash"] != esperado:
                    return False, f["id"]
                anterior = f["hash"]
                total += 1
        return True, total

    # --- Solicitudes ----------------------------------------------------

    @staticmethod
    def _solicitud(fila):
        if fila is None:
            return None
        datos = dict(fila)
        for clave in _JSON:
            if datos.get(clave) is not None:
                datos[clave] = json.loads(datos[clave])
        return datos

    def solicitud(self, solicitud_id, db=None):
        consulta = (
            "SELECT s.*, us.nombre AS solicitante, ua.nombre AS aprobador, "
            "ue.nombre AS ejecutor FROM solicitudes s "
            "JOIN usuarios us ON us.id = s.solicitante_id "
            "LEFT JOIN usuarios ua ON ua.id = s.aprobador_id "
            "LEFT JOIN usuarios ue ON ue.id = s.ejecutor_id "
            "WHERE s.id = ?")
        if db is not None:
            return self._solicitud(db.execute(consulta,
                                              (solicitud_id,)).fetchone())
        with self.transaccion() as db:
            return self._solicitud(db.execute(consulta,
                                              (solicitud_id,)).fetchone())

    def solicitudes(self, estado=None, limite=200):
        consulta = (
            "SELECT s.id, s.titulo, s.conexion, s.script_nombre, s.estado, "
            "s.creada, s.actualizada, us.nombre AS solicitante, "
            "ua.nombre AS aprobador, "
            "json_extract(s.informe, '$.veredicto.nivel') AS veredicto, "
            "json_extract(s.informe, '$.filas_previstas') AS filas_previstas "
            "FROM solicitudes s "
            "JOIN usuarios us ON us.id = s.solicitante_id "
            "LEFT JOIN usuarios ua ON ua.id = s.aprobador_id ")
        parametros = []
        if estado:
            consulta += "WHERE s.estado = ? "
            parametros.append(estado)
        consulta += "ORDER BY s.id DESC LIMIT ?"
        parametros.append(limite)
        with self.transaccion() as db:
            return [dict(f) for f in db.execute(consulta, parametros)]

    def insertar_solicitud(self, db, datos):
        campos = dict(datos)
        for clave in _JSON:
            if clave in campos and campos[clave] is not None:
                campos[clave] = json.dumps(campos[clave], ensure_ascii=False)
        campos.setdefault("creada", ahora())
        campos["actualizada"] = campos["creada"]
        columnas = ", ".join(campos)
        marcas = ", ".join("?" for _ in campos)
        cur = db.execute(f"INSERT INTO solicitudes ({columnas}) "
                         f"VALUES ({marcas})", list(campos.values()))
        return cur.lastrowid

    def actualizar_solicitud(self, db, solicitud_id, desde=None, **campos):
        """Actualiza campos. Con `desde`, solo si el estado es uno de esos.

        Devuelve True si se actualizó (la transición era válida).
        """
        for clave in _JSON:
            if clave in campos and campos[clave] is not None:
                campos[clave] = json.dumps(campos[clave], ensure_ascii=False)
        campos["actualizada"] = ahora()
        asignaciones = ", ".join(f"{c} = ?" for c in campos)
        consulta = f"UPDATE solicitudes SET {asignaciones} WHERE id = ?"
        parametros = list(campos.values()) + [solicitud_id]
        if desde:
            consulta += f" AND estado IN ({', '.join('?' for _ in desde)})"
            parametros += list(desde)
        return db.execute(consulta, parametros).rowcount == 1
