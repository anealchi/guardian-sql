// SQL Guardian — interfaz web sin dependencias.
// Todo el contenido se crea con nodos DOM y textContent (nunca innerHTML
// con datos), así que un script o un comentario no pueden inyectar HTML.

"use strict";

const estado = { usuario: null, conexiones: [] };
const principal = document.getElementById("principal");

const NOMBRES_ESTADO = {
  pendiente: "Pendiente de aprobar",
  aprobada: "Aprobada",
  ejecutando: "Ejecutando",
  ejecutada: "Ejecutada",
  no_aplicada: "No aplicada",
  fallida: "Fallida",
  rechazada: "Rechazada",
  cancelada: "Cancelada",
  deshecha: "Deshecha",
};

const NOMBRES_ROL = {
  solicitante: "solicitante",
  aprobador: "aprobador",
  admin: "administrador",
};

const NOMBRES_VEREDICTO = {
  bloqueado: "Bloqueado",
  revisar: "Revisar",
  ok: "Sin riesgos altos",
};

const EVENTOS = {
  crear: "Envió la solicitud",
  aprobar: "Aprobó",
  rechazar: "Rechazó",
  cancelar: "Canceló la solicitud",
  comentar: "Comentó",
  reanalizar: "Volvió a analizar el script",
  editar: "Editó la solicitud",
  ejecucion_iniciada: "Empezó la ejecución",
  ejecucion_ejecutada: "Ejecutó y aplicó los cambios",
  ejecucion_no_aplicada: "Ejecutó, pero no se aplicó nada",
  ejecucion_fallida: "La ejecución falló",
  ejecucion_recuperada: "Comprobó en la base de datos la ejecución " +
    "interrumpida",
  deshacer: "Deshizo la ejecución",
  deshacer_fallido: "No pudo deshacer la ejecución",
  acceso: "Inició sesión",
  acceso_fallido: "Intento de acceso fallido",
  crear_usuario: "Creó un usuario",
  modificar_usuario: "Modificó un usuario",
  activar_usuario: "Activó un usuario",
  desactivar_usuario: "Desactivó un usuario",
};

// Texto de un evento del historial o de la auditoría.
function nombreEvento(e) {
  if (e.accion === "aprobar" && e.detalle && e.detalle.propia) {
    return "Aprobó su propia solicitud";
  }
  return EVENTOS[e.accion] || e.accion;
}

// Campos que puede listar el evento "editar" en `cambios`.
const CAMPOS_EDITADOS = {
  conexion: "la base de datos",
  motivo: "el motivo",
  opciones: "las opciones",
  script_nombre: "el nombre del script",
  sql: "el script",
  titulo: "el título",
};

const EVENTOS_BUENOS =new Set(["aprobar", "ejecucion_ejecutada"]);
const EVENTOS_MALOS = new Set([
  "rechazar", "ejecucion_no_aplicada", "ejecucion_fallida",
  "deshacer_fallido", "acceso_fallido",
]);

// --- Utilidades ---------------------------------------------------------

function h(etiqueta, atributos, ...hijos) {
  const nodo = document.createElement(etiqueta);
  for (const [clave, valor] of Object.entries(atributos || {})) {
    if (valor === null || valor === undefined || valor === false) continue;
    if (clave === "clase") nodo.className = valor;
    else if (/^al[A-Z]/.test(clave)) nodo.addEventListener(
      clave.slice(2).toLowerCase(), valor);
    else if (valor === true) nodo.setAttribute(clave, "");
    else nodo.setAttribute(clave, valor);
  }
  for (const hijo of hijos.flat()) {
    if (hijo === null || hijo === undefined || hijo === false) continue;
    nodo.append(hijo instanceof Node ? hijo : document.createTextNode(hijo));
  }
  return nodo;
}

function vaciar(nodo, ...hijos) {
  nodo.replaceChildren(...hijos.flat().filter(Boolean));
}

const formatoFecha = new Intl.DateTimeFormat("es-ES", {
  dateStyle: "medium", timeStyle: "short",
});

function fecha(iso) {
  return iso ? formatoFecha.format(new Date(iso)) : "—";
}

function numero(n) {
  return n === null || n === undefined ? "—" : n.toLocaleString("es-ES");
}

function filasTexto(n) {
  return n === 1 ? "fila" : "filas";
}

// "fila prevista" / "filas previstas": el participio concuerda con n.
function filasYParticipio(n, participio) {
  return n === 1 ? `fila ${participio}` : `filas ${participio}s`;
}

// "Cambió el script y el título." y, si se volvió a analizar, las filas.
function textoEdicion(d) {
  const nombres = d.cambios.map((c) => CAMPOS_EDITADOS[c] || c);
  const lista = nombres.length > 1
    ? `${nombres.slice(0, -1).join(", ")} y ${nombres[nombres.length - 1]}`
    : nombres[0];
  let texto = `Cambió ${lista}.`;
  if (d.veredicto) {
    texto += ` Se volvió a analizar: ${numero(d.filas_previstas)} ` +
      `${filasYParticipio(d.filas_previstas, "prevista")}.`;
  }
  return texto;
}

const moverMenos = matchMedia("(prefers-reduced-motion: reduce)");

function avisar(texto, esError = false) {
  const aviso = document.querySelector(".aviso");
  aviso.textContent = texto;
  aviso.classList.toggle("error", esError);
  aviso.classList.remove("saliendo");
  aviso.hidden = false;
  clearTimeout(avisar.temporizador);
  avisar.temporizador = setTimeout(() => {
    // Se desvanece antes de ocultarse; con movimiento reducido, de golpe.
    if (moverMenos.matches) { aviso.hidden = true; return; }
    aviso.classList.add("saliendo");
    avisar.temporizador = setTimeout(() => {
      aviso.hidden = true;
      aviso.classList.remove("saliendo");
    }, 200);
  }, 6000);
}

// Marca <main> para que la vista que se pinta a continuación entre con su
// animación (ver «Entrada de cada vista» en estilos.css). La marca se
// quita al terminar: lo que se repinta después ya no se anima.
function entrada() {
  clearTimeout(entrada.temporizador);
  principal.classList.add("entrando");
}

function finEntrada() {
  clearTimeout(entrada.temporizador);
  entrada.temporizador = setTimeout(
    () => principal.classList.remove("entrando"), 1200);
}

class ErrorApi extends Error {
  constructor(mensaje, codigo) { super(mensaje); this.codigo = codigo; }
}

async function api(metodo, ruta, cuerpo) {
  const opciones = { method: metodo, headers: {}, credentials: "same-origin" };
  if (metodo !== "GET") opciones.headers["X-Guardian"] = "1";
  if (cuerpo !== undefined) {
    opciones.headers["Content-Type"] = "application/json";
    opciones.body = JSON.stringify(cuerpo);
  }
  const respuesta = await fetch(ruta, opciones);
  let datos = null;
  try { datos = await respuesta.json(); } catch { datos = null; }
  if (respuesta.status === 401 && ruta !== "/api/sesion") {
    estado.usuario = null;
    mostrarAcceso();
    throw new ErrorApi("Tu sesión ha caducado. Vuelve a iniciar sesión.", 401);
  }
  if (!respuesta.ok) {
    let detalle = datos && datos.detail;
    if (Array.isArray(detalle)) {
      detalle = detalle.map((d) => d.msg).join(". ");
    }
    throw new ErrorApi(detalle || `Error ${respuesta.status}.`,
      respuesta.status);
  }
  return datos;
}

function esAprobador() {
  return estado.usuario && ["aprobador", "admin"].includes(estado.usuario.rol);
}

function etiquetaEstado(nombre) {
  return h("span", { clase: `estado estado-${nombre}` },
    NOMBRES_ESTADO[nombre] || nombre);
}

function etiquetaNivel(nivel) {
  const clase = { "crítico": "critico" }[nivel] || nivel;
  return h("span", { clase: `nivel nivel-${clase}` }, nivel);
}

// Botón que se desactiva mientras espera y muestra el error si falla.
function botonAccion(texto, clase, accion) {
  const boton = h("button", { type: "button", clase: `boton ${clase}` },
    texto);
  boton.addEventListener("click", async () => {
    boton.disabled = true;
    try { await accion(); } catch (error) {
      if (error.codigo !== 401) avisar(error.message, true);
    } finally { boton.disabled = false; }
  });
  return boton;
}

// Diálogo de confirmación con comentario opcional u obligatorio.
function confirmar({ titulo, texto, boton, clase = "boton-principal",
  comentario = null }) {
  return new Promise((resolver) => {
    const area = comentario ? h("textarea", {
      rows: "3", "aria-label": comentario.etiqueta,
      placeholder: comentario.etiqueta,
    }) : null;
    const error = h("p", { clase: "error-formulario", hidden: true });
    const dialogo = h("dialog", { "aria-labelledby": "dialogo-titulo" },
      h("h2", { id: "dialogo-titulo" }, titulo),
      h("p", {}, texto),
      area, error,
      h("div", { clase: "acciones" },
        h("button", {
          type: "button", clase: "boton",
          alClick: () => { dialogo.close(); resolver(null); },
        }, "Volver"),
        h("button", {
          type: "button", clase: `boton ${clase}`,
          alClick: () => {
            const valor = area ? area.value.trim() : "";
            if (comentario && comentario.obligatorio && !valor) {
              error.textContent = "Escribe el motivo antes de continuar.";
              error.hidden = false;
              area.focus();
              return;
            }
            dialogo.close();
            resolver({ comentario: valor });
          },
        }, boton)));
    dialogo.addEventListener("cancel", () => resolver(null));
    dialogo.addEventListener("close", () => dialogo.remove());
    document.body.append(dialogo);
    dialogo.showModal();
    (area || dialogo.querySelector(".boton")).focus();
  });
}

// --- Navegación -----------------------------------------------------------

const rutas = [
  [/^#?\/?$/, "bandeja", verBandeja],
  [/^#\/nueva$/, "nueva", verNueva],
  [/^#\/solicitud\/(\d+)$/, "bandeja", verDetalle],
  [/^#\/solicitud\/(\d+)\/editar$/, "bandeja", verEditar],
  [/^#\/auditoria$/, "auditoria", verAuditoria],
  [/^#\/usuarios$/, "usuarios", verUsuarios],
];

async function navegar() {
  if (!estado.usuario) return;
  const hash = location.hash || "#/";
  for (const [patron, seccion, vista] of rutas) {
    const coincide = hash.match(patron);
    if (!coincide) continue;
    document.querySelectorAll(".menu a").forEach((a) => {
      if (a.dataset.seccion === seccion) {
        a.setAttribute("aria-current", "page");
        // En móvil el menú se desplaza en horizontal: que se vea la actual.
        a.scrollIntoView({ block: "nearest", inline: "nearest" });
      } else a.removeAttribute("aria-current");
    });
    entrada();
    vaciar(principal, h("p", { clase: "cargando" }, "Cargando…"));
    try {
      await vista(...coincide.slice(1));
    } catch (error) {
      if (error.codigo !== 401) {
        vaciar(principal, h("div", { clase: "vacio" },
          h("h2", {}, "No se ha podido cargar esta página"),
          h("p", {}, error.message),
          h("a", { href: "#/" }, "Volver a las solicitudes")));
      }
    }
    finEntrada();
    return;
  }
  location.hash = "#/";
}

window.addEventListener("hashchange", navegar);

// --- Acceso ---------------------------------------------------------------

function mostrarAcceso() {
  document.querySelector(".barra").hidden = true;
  const error = h("p", { clase: "error-formulario", hidden: true,
    role: "alert" });
  const nombre = h("input", { type: "text", name: "nombre",
    autocomplete: "username", required: true, autofocus: true });
  const clave = h("input", { type: "password", name: "clave",
    autocomplete: "current-password", required: true });
  const boton = h("button", { type: "submit",
    clase: "boton boton-principal" }, "Entrar");
  const formulario = h("form", { clase: "formulario" },
    h("label", { clase: "campo" }, h("span", {}, "Usuario"), nombre),
    h("label", { clase: "campo" }, h("span", {}, "Contraseña"), clave),
    error, boton);
  formulario.addEventListener("submit", async (evento) => {
    evento.preventDefault();
    boton.disabled = true;
    error.hidden = true;
    try {
      estado.usuario = await api("POST", "/api/sesion", {
        nombre: nombre.value, clave: clave.value,
      });
      await arrancar();
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
      clave.select();
    } finally { boton.disabled = false; }
  });
  const logo = document.querySelector(".marca svg").cloneNode(true);
  entrada();
  vaciar(principal, h("section", { clase: "acceso" },
    h("div", { clase: "acceso-presentacion" },
      h("p", { clase: "acceso-marca" }, logo, "SQL Guardian"),
      h("h1", {}, "Ningún script llega a producción sin que otra " +
        "persona vea lo que va a cambiar."),
      h("ol", { clase: "acceso-pasos" },
        h("li", {}, h("span", {}, h("b", {}, "Envías el script."),
          " Se analiza contra la base de datos: filas, triggers y riesgos.")),
        h("li", {}, h("span", {}, h("b", {}, "Un aprobador lo aprueba"),
          " con esos números de filas exactos.")),
        h("li", {}, h("span", {}, h("b", {}, "Se ejecuta con respaldo."),
          " Si las filas no coinciden, no se aplica nada; si algo sale " +
          "mal, se deshace.")))),
    h("div", { clase: "acceso-tarjeta" },
      h("h2", {}, "Iniciar sesión"), formulario)));
  finEntrada();
  nombre.focus();
}

function pintarCuenta() {
  const barra = document.querySelector(".barra");
  barra.querySelector(".cuenta-nombre").textContent =
    estado.usuario.nombre_completo || estado.usuario.nombre;
  barra.querySelector(".cuenta-rol").textContent =
    NOMBRES_ROL[estado.usuario.rol];
  barra.querySelector("[data-solo-admin]").hidden =
    estado.usuario.rol !== "admin";
}

async function arrancar() {
  document.querySelector(".barra").hidden = false;
  pintarCuenta();
  estado.conexiones = await api("GET", "/api/conexiones");
  await navegar();
}

document.getElementById("salir").addEventListener("click", async () => {
  try { await api("DELETE", "/api/sesion"); } catch { /* da igual */ }
  estado.usuario = null;
  mostrarAcceso();
});

// --- Bandeja --------------------------------------------------------------

const FILTROS = [
  ["todas", "Todas", null],
  ["pendiente", "Pendientes", ["pendiente"]],
  ["aprobada", "Aprobadas", ["aprobada"]],
  ["ejecutada", "Ejecutadas", ["ejecutada"]],
  ["problemas", "Sin aplicar o fallidas", ["no_aplicada", "fallida"]],
  ["cerradas", "Cerradas", ["rechazada", "cancelada", "deshecha"]],
];

async function verBandeja() {
  const solicitudes = await api("GET", "/api/solicitudes");
  let filtro = sessionStorage.getItem("filtro") || "todas";
  if (!FILTROS.some(([clave]) => clave === filtro)) filtro = "todas";
  // .bandeja hace que la página ocupe justo la pantalla y que solo se
  // desplace la tabla (ver estilos.css).
  const cuerpo = h("div", { clase: "bandeja" });

  const pintar = () => {
    const estados = FILTROS.find(([clave]) => clave === filtro)[2];
    const visibles = estados
      ? solicitudes.filter((s) => estados.includes(s.estado))
      : solicitudes;
    const botones = FILTROS.map(([clave, texto, lista]) => {
      const total = lista
        ? solicitudes.filter((s) => lista.includes(s.estado)).length
        : solicitudes.length;
      return h("button", {
        type: "button", "aria-pressed": String(clave === filtro),
        alClick: () => {
          filtro = clave;
          sessionStorage.setItem("filtro", clave);
          pintar();
        },
      }, texto, h("span", { clase: "cuenta-filtro" }, String(total)));
    });
    vaciar(cuerpo,
      h("div", { clase: "filtros", role: "group",
        "aria-label": "Filtrar por estado" }, botones),
      visibles.length ? tablaSolicitudes(visibles) : bandejaVacia(filtro));
  };

  vaciar(principal,
    h("div", { clase: "cabecera" },
      h("div", {},
        h("h1", {}, "Solicitudes"),
        h("p", {}, "Cada script se analiza contra la base de datos al " +
          "enviarlo. Lo aprueba un aprobador y se ejecuta con respaldo.")),
      h("a", { clase: "boton boton-principal", href: "#/nueva" },
        "Nueva solicitud")),
    cuerpo);
  pintar();
}

function bandejaVacia(filtro) {
  if (filtro !== "todas") {
    return h("div", { clase: "vacio" },
      h("p", {}, "No hay solicitudes en este estado."));
  }
  return h("div", { clase: "vacio" },
    h("h2", {}, "Aún no hay solicitudes"),
    h("p", {}, "Envía el primer script: verás qué tablas toca, cuántas " +
      "filas cambia y qué riesgos tiene antes de que nadie lo apruebe."),
    h("a", { clase: "boton boton-principal", href: "#/nueva" },
      "Nueva solicitud"));
}

function tablaSolicitudes(solicitudes) {
  const filas = solicitudes.map((s) => {
    const enlace = h("a", { clase: "titulo-solicitud",
      href: `#/solicitud/${s.id}` }, s.titulo);
    const fila = h("tr", {},
      h("td", { clase: "num secundario c-id" }, String(s.id)),
      h("td", { clase: "c-titulo" }, enlace,
        h("div", { clase: "secundario pequeno" },
          `${s.script_nombre} · ${s.conexion}`)),
      h("td", { clase: "c-estado" }, etiquetaEstado(s.estado)),
      h("td", { clase: "c-riesgo" }, s.veredicto
        ? h("span", { clase: `nivel nivel-${s.veredicto === "bloqueado"
          ? "critico" : s.veredicto === "revisar" ? "alto" : "ok"}` },
        NOMBRES_VEREDICTO[s.veredicto]) : "—"),
      h("td", { clase: "num c-filas" }, numero(s.filas_previstas),
        s.filas_previstas === null ? null : h("span", { clase: "solo-movil" },
          s.filas_previstas === 1 ? " fila prevista" : " filas previstas")),
      h("td", { clase: "ocultable c-quien" }, s.solicitante),
      h("td", { clase: "ocultable secundario c-fecha" },
        fecha(s.actualizada)));
    fila.addEventListener("click", (evento) => {
      if (evento.target.closest("a")) return;
      location.hash = `#/solicitud/${s.id}`;
    });
    return fila;
  });
  return h("div", { clase: "tabla-contenedor" },
    h("table", { clase: "tabla tabla-solicitudes" },
      h("thead", {}, h("tr", {},
        h("th", { clase: "num", scope: "col" }, "Nº"),
        h("th", { scope: "col" }, "Solicitud"),
        h("th", { scope: "col" }, "Estado"),
        h("th", { scope: "col" }, "Riesgo"),
        h("th", { clase: "num", scope: "col" }, "Filas previstas"),
        h("th", { clase: "ocultable", scope: "col" }, "Pedida por"),
        h("th", { clase: "ocultable", scope: "col" }, "Último cambio"))),
      h("tbody", {}, filas)));
}

// --- Nueva solicitud y edición --------------------------------------------

function verNueva() {
  formularioSolicitud(null);
}

// Solo quien la pidió y mientras está pendiente; si no, vuelve al detalle.
async function verEditar(id) {
  const s = await api("GET", `/api/solicitudes/${id}`);
  if (s.estado !== "pendiente" || s.solicitante_id !== estado.usuario.id) {
    location.hash = `#/solicitud/${id}`;
    return;
  }
  formularioSolicitud(s);
}

// Formulario de una solicitud: vacío para crearla o, con `s`, relleno con
// sus datos para editarla.
function formularioSolicitud(s) {
  const editando = Boolean(s);
  const textoEnviar = editando ? "Guardar cambios" : "Analizar y enviar";
  const conexion = h("select", { name: "conexion", required: true },
    estado.conexiones.map((c) => h("option", { value: c.nombre },
      c.nombre)));
  const notaConexion = h("small");
  const ignorar = h("input", { type: "text", name: "ignorar",
    placeholder: "parte_trabajo_tarea_log" });
  const actualizarConexion = () => {
    const c = estado.conexiones.find((x) => x.nombre === conexion.value);
    if (!c) return;
    const partes = [c.simular
      ? "Se analiza ejecutando el script y deshaciéndolo: números exactos."
      : "Se analiza contando filas, sin ejecutar nada."];
    if (c.ignorar_tablas.length) {
      partes.push(`Ignora siempre: ${c.ignorar_tablas.join(", ")}.`);
    }
    notaConexion.textContent = partes.join(" ");
  };
  conexion.addEventListener("change", actualizarConexion);
  if (editando) conexion.value = s.conexion;
  actualizarConexion();

  const titulo = h("input", { type: "text", name: "titulo", required: true,
    maxlength: "200", placeholder: "Borrar tareas duplicadas del parte" });
  const motivo = h("textarea", { name: "motivo", rows: "3",
    placeholder: "Qué problema resuelve y cómo se ha comprobado." });
  const sql = h("textarea", { name: "sql", clase: "sql", required: true,
    spellcheck: "false", placeholder: "BEGIN;\n…\nROLLBACK;" });
  let nombreScript = editando ? s.script_nombre : "script.sql";
  const archivo = h("input", { type: "file", accept: ".sql,text/plain" });
  archivo.addEventListener("change", async () => {
    const elegido = archivo.files[0];
    if (!elegido) return;
    sql.value = await elegido.text();
    nombreScript = elegido.name;
    if (!titulo.value) titulo.value = elegido.name.replace(/\.sql$/i, "")
      .replace(/[_-]+/g, " ");
  });
  const indirectos = h("input", { type: "checkbox", name: "indirectos" });
  const forzar = h("input", { type: "checkbox", name: "forzar" });
  const error = h("p", { clase: "error-formulario", hidden: true,
    role: "alert" });
  const enviar = h("button", { type: "submit",
    clase: "boton boton-principal" }, textoEnviar);
  if (editando) {
    // Las tablas que la conexión ignora siempre ya se añaden solas.
    const deConexion = (estado.conexiones.find(
      (c) => c.nombre === s.conexion) || { ignorar_tablas: [] })
      .ignorar_tablas;
    titulo.value = s.titulo;
    motivo.value = s.motivo;
    sql.value = s.sql;
    ignorar.value = (s.opciones.ignorar_tablas || [])
      .filter((t) => !deConexion.includes(t)).join(", ");
    indirectos.checked = Boolean(s.opciones.permitir_indirectos);
    forzar.checked = Boolean(s.opciones.forzar);
  }
  const volver = editando ? `#/solicitud/${s.id}` : "#/";

  const formulario = h("form", { clase: "formulario" },
    h("div", { clase: "fila-campos" },
      h("label", { clase: "campo" }, h("span", {}, "Título"), titulo),
      h("label", { clase: "campo" }, h("span", {}, "Base de datos"),
        conexion, notaConexion)),
    h("label", { clase: "campo" }, h("span", {}, "Motivo"), motivo),
    h("div", { clase: "campo" },
      h("label", { for: "sql-nuevo" }, "Script"),
      h("div", { clase: "selector-archivo" }, archivo,
        h("small", {}, "o pégalo debajo.")),
      sql),
    h("label", { clase: "campo" },
      h("span", {}, "Tablas cuyos cambios indirectos no importan"),
      ignorar,
      h("small", {}, "Separadas por comas. Normalmente, tablas de log que " +
        "rellenan los triggers.")),
    h("label", { clase: "casilla" }, indirectos, h("span", {},
      "Permitir cambios sin respaldo de triggers y cascadas",
      h("small", {}, "Esos cambios no se podrán deshacer con el botón " +
        "Deshacer."))),
    h("label", { clase: "casilla" }, forzar, h("span", {},
      "Ejecutar también lo que no se puede respaldar",
      h("small", {}, "Cambios de estructura, TRUNCATE, DO… Quien apruebe " +
        "lo verá marcado."))),
    error,
    h("div", { clase: "acciones-formulario" }, enviar,
      h("a", { clase: "boton boton-texto", href: volver }, "Cancelar")));
  sql.id = "sql-nuevo";

  formulario.addEventListener("submit", async (evento) => {
    evento.preventDefault();
    enviar.disabled = true;
    enviar.textContent = editando ? "Guardando…" : "Analizando el script…";
    error.hidden = true;
    try {
      const guardada = await api(editando ? "PATCH" : "POST",
        editando ? `/api/solicitudes/${s.id}` : "/api/solicitudes", {
        titulo: titulo.value, motivo: motivo.value,
        conexion: conexion.value, script_nombre: nombreScript,
        sql: sql.value,
        ignorar_tablas: ignorar.value.split(",").map((t) => t.trim())
          .filter(Boolean),
        permitir_indirectos: indirectos.checked, forzar: forzar.checked,
      });
      if (editando) avisar("Cambios guardados.");
      location.hash = `#/solicitud/${guardada.id}`;
    } catch (e) {
      if (e.codigo === 401) return;
      error.textContent = e.message;
      error.hidden = false;
      enviar.disabled = false;
      enviar.textContent = textoEnviar;
    }
  });

  vaciar(principal,
    editando ? h("nav", { clase: "migas", "aria-label": "Ruta" },
      h("a", { href: volver },
        h("span", { clase: "flecha", "aria-hidden": "true" }, "←"),
        `Nº ${s.id}`)) : null,
    h("div", { clase: "cabecera" }, h("div", {},
      h("h1", {}, editando ? "Editar solicitud" : "Nueva solicitud"),
      h("p", {}, editando
        ? "Si cambias el script o la base de datos, se vuelve a analizar. " +
          "Solo se puede editar mientras nadie la haya aprobado."
        : "Al enviarla se analiza contra la base de datos elegida. " +
          "Nada se aplica hasta que un aprobador la apruebe y se ejecute."))),
    formulario);
  titulo.focus();
}

// --- Detalle --------------------------------------------------------------

async function verDetalle(id) {
  const s = await api("GET", `/api/solicitudes/${id}`);
  pintarDetalle(s);
}

function accionesDisponibles(s) {
  const yo = estado.usuario;
  const mia = s.solicitante_id === yo.id;
  const lista = [];
  const hacer = (accion, cuerpo) => async () => {
    const actualizada = await api("POST",
      `/api/solicitudes/${s.id}/${accion}`, cuerpo);
    pintarDetalle(actualizada);
    return actualizada;
  };

  if (s.estado === "aprobada" && (mia || esAprobador())) {
    lista.push(botonAccion("Ejecutar", "boton-principal", async () => {
      const ok = await confirmar({
        titulo: "¿Ejecutar en " + s.conexion + "?",
        texto: "Se ejecuta en una transacción con respaldo. Si alguna " +
          "sentencia cambia un número de filas distinto del aprobado, se " +
          "deshace todo y no se aplica nada.",
        boton: "Ejecutar ahora",
      });
      if (!ok) return;
      const r = await hacer("ejecutar")();
      avisar(r.estado === "ejecutada" ? "Cambios aplicados."
        : "No se ha aplicado nada. Revisa el resultado.", r.estado !== "ejecutada");
    }));
  }
  if (s.estado === "ejecutando" && (mia || esAprobador())) {
    // Si el servidor se detuvo a mitad, la solicitud se queda aquí: esto
    // mira en PostgreSQL si hubo COMMIT.
    lista.push(botonAccion("Comprobar la ejecución", "", async () => {
      const r = await hacer("recuperar")();
      avisar(r.estado === "ejecutada" ? "Los cambios están aplicados."
        : r.estado === "deshecha" ? "La ejecución ya estaba deshecha."
          : "No se aplicó nada.", r.estado === "no_aplicada");
    }));
  }
  if (s.estado === "pendiente" && esAprobador()) {
    lista.push(botonAccion("Aprobar", "boton-principal", async () => {
      const ok = await confirmar({
        titulo: mia ? "¿Aprobar tu propia solicitud?"
          : "¿Aprobar esta solicitud?",
        texto: "Apruebas este script exacto con los números de filas del " +
          "informe. Si cambian al ejecutarlo, no se aplicará." +
          (mia ? " Nadie más lo revisará: en el historial y la auditoría " +
            "constará que la aprobaste tú." : ""),
        boton: "Aprobar",
        comentario: { etiqueta: "Comentario (opcional)" },
      });
      // Con la versión que se está viendo: si alguien la ha editado
      // mientras tanto, el servidor no la aprueba.
      if (ok) {
        await hacer("aprobar", { ...ok, version: s.version })();
        avisar("Aprobada.");
      }
    }));
  }
  if (s.estado === "pendiente" && mia) {
    lista.push(h("a", { clase: "boton", href: `#/solicitud/${s.id}/editar` },
      "Editar"));
  }
  if (s.estado === "ejecutada" && esAprobador()) {
    lista.push(botonAccion("Deshacer", "boton-peligro", async () => {
      const ok = await confirmar({
        titulo: "¿Deshacer la ejecución?",
        texto: "Se restauran las filas respaldadas que siguen como las " +
          "dejó la ejecución. Las que alguien haya cambiado después se " +
          "dejan como están.",
        boton: "Deshacer", clase: "boton-peligro",
        comentario: { etiqueta: "Motivo", obligatorio: true },
      });
      if (ok) { await hacer("deshacer", ok)(); avisar("Ejecución deshecha."); }
    }));
  }
  if (["pendiente", "aprobada", "no_aplicada"].includes(s.estado)
      && (mia || esAprobador())) {
    lista.push(botonAccion("Volver a analizar", "", async () => {
      const r = await hacer("reanalizar")();
      avisar(s.estado === "aprobada"
        ? "Analizada de nuevo. La aprobación anterior queda anulada."
        : "Analizada de nuevo.");
      return r;
    }));
  }
  if (["pendiente", "aprobada"].includes(s.estado) && esAprobador()) {
    lista.push(botonAccion("Rechazar", "boton-peligro", async () => {
      const ok = await confirmar({
        titulo: "¿Rechazar esta solicitud?",
        texto: "Quien la pidió verá el motivo en el historial.",
        boton: "Rechazar", clase: "boton-peligro",
        comentario: { etiqueta: "Motivo del rechazo", obligatorio: true },
      });
      if (ok) { await hacer("rechazar", ok)(); avisar("Rechazada."); }
    }));
  }
  if (["pendiente", "aprobada"].includes(s.estado)
      && (mia || yo.rol === "admin")) {
    lista.push(botonAccion("Cancelar solicitud", "boton-texto", async () => {
      const ok = await confirmar({
        titulo: "¿Cancelar la solicitud?",
        texto: "No se podrá aprobar ni ejecutar.",
        boton: "Cancelar solicitud", clase: "boton-peligro",
      });
      if (ok) { await hacer("cancelar", ok)(); avisar("Cancelada."); }
    }));
  }
  return lista;
}

function pintarDetalle(s) {
  const informe = s.informe || {};
  const opciones = s.opciones || {};
  const meta = [
    ["Base de datos", s.conexion],
    ["Script", s.script_nombre],
    ["Pedida por", s.solicitante],
    s.aprobador ? ["Aprobada por", s.aprobador] : null,
    s.ejecutor ? ["Ejecutada por", s.ejecutor] : null,
  ].filter(Boolean);

  const avisosOpciones = [];
  if (opciones.forzar) avisosOpciones.push(
    "Se pidió ejecutar también lo que no se puede respaldar (forzar).");
  if (opciones.permitir_indirectos) avisosOpciones.push(
    "Se permiten cambios de triggers y cascadas sin respaldo.");
  if ((opciones.ignorar_tablas || []).length) avisosOpciones.push(
    `Se ignoran los cambios indirectos en ${opciones.ignorar_tablas.join(", ")}.`);

  vaciar(principal,
    h("nav", { clase: "migas", "aria-label": "Ruta" },
      h("a", { href: "#/" },
        h("span", { clase: "flecha", "aria-hidden": "true" }, "←"),
        "Solicitudes")),
    // Fuera de .detalle-cabecera: sticky solo funciona dentro de su
    // contenedor, y así se queda arriba mientras se recorre el script.
    h("div", { clase: "cabecera cabecera-detalle" },
      h("div", {},
        h("h1", {}, s.titulo),
        h("div", { clase: "linea-meta" },
          h("span", {}, `Nº ${s.id}`), etiquetaEstado(s.estado),
          h("span", {}, `Enviada ${fecha(s.creada)}`))),
      h("div", { clase: "acciones" }, accionesDisponibles(s))),
    h("div", { clase: "detalle-cabecera" },
      h("div", { clase: "linea-meta" }, meta.map(([etiqueta, valor]) =>
        h("span", {}, `${etiqueta}: `, h("b", {}, valor)))),
      s.motivo ? h("p", { clase: "motivo" }, s.motivo) : null),
    bloqueVeredicto(s, informe),
    bloqueResultado(s),
    informe.error ? h("div", { clase: "nota-bloque critico" },
      h("h3", {}, "No se ha podido calcular el impacto"),
      h("p", {}, informe.error),
      h("p", { clase: "secundario" }, "Sin impacto no se puede aprobar. " +
        "Usa Volver a analizar cuando la base de datos responda.")) : null,
    avisosOpciones.length ? h("div", { clase: "nota-bloque" +
      (opciones.forzar || opciones.permitir_indirectos ? " alto" : "") },
      h("h3", {}, "Opciones de ejecución"),
      h("ul", {}, avisosOpciones.map((t) => h("li", {}, t)))) : null,
    scriptRevisado(s, informe),
    h("div", { clase: "columnas" },
      h("div", {}, seccionHallazgosGenerales(informe),
        seccionTriggers(informe)),
      seccionHistorial(s)));
}

function bloqueVeredicto(s, informe) {
  if (!informe.veredicto) return null;
  const nivel = informe.veredicto.nivel;
  const impacto = informe.impacto;
  const previstas = informe.filas_previstas;
  const detalle = [];
  if (impacto) {
    detalle.push(impacto.modo === "simulacion"
      ? "Simulado en la base de datos y deshecho."
      : "Contado en la base de datos sin ejecutar nada.");
    detalle.push(`PostgreSQL ${impacto.servidor}.`);
  }
  if (informe.tablas_modificadas.length) {
    detalle.push(`Modifica ${informe.tablas_modificadas.join(", ")}.`);
  }
  if (s.informe_fecha) detalle.push(`Analizado ${fecha(s.informe_fecha)}.`);
  const resultado = s.resultado || {};
  const aplicadas = ["ejecutada", "deshecha"].includes(s.estado)
    && resultado.filas_respaldadas !== undefined;
  const cifra = aplicadas ? resultado.filas_respaldadas : previstas;
  if (aplicadas && cifra !== previstas) {
    detalle.unshift(`Se preveían ${numero(previstas)}.`);
  }
  return h("div", { clase: `veredicto veredicto-${nivel}` },
    h("div", { clase: "cifra" }, numero(cifra),
      h("small", {}, filasYParticipio(cifra,
        aplicadas ? "cambiada" : "prevista"))),
    h("p", { clase: "veredicto-texto" }, informe.veredicto.texto),
    h("p", { clase: "secundario" }, detalle.join(" ")));
}

function bloqueResultado(s) {
  const r = s.resultado;
  if (!r) return null;
  const partes = [];
  let clase = "aplicado";
  let titulo = "Cambios aplicados";
  if (s.estado === "deshecha") {
    clase = "";
    titulo = "Ejecución deshecha";
  } else if (s.estado !== "ejecutada") {
    clase = "critico";
    titulo = s.estado === "fallida" ? "La ejecución falló"
      : "No se ha aplicado nada";
  }
  if (r.motivo) partes.push(h("p", {}, r.motivo));
  if (r.discrepancias && r.discrepancias.length) {
    partes.push(h("ul", {}, r.discrepancias.map((d) => h("li", {},
      `Línea ${d.linea}, ${d.operacion} ${d.tabla || ""}: se aprobaron ` +
      `${numero(d.aprobadas)} ${filasTexto(d.aprobadas)} y ahora serían ` +
      `${numero(d.reales)}.`))));
  }
  if (r.filas_respaldadas !== undefined && s.estado !== "fallida") {
    partes.push(h("p", { clase: "secundario" },
      `${numero(r.filas_respaldadas)} ${filasTexto(r.filas_respaldadas)} ` +
      `respaldadas` + (s.ejecucion_id ? ` en la ejecución ${s.ejecucion_id}.`
        : ".")));
  }
  if (r.bloqueos && r.bloqueos.length && s.estado !== "ejecutada") {
    partes.push(h("p", {}, "No se pueden respaldar:"),
      h("ul", {}, r.bloqueos.map((b) => h("li", {},
        b.linea ? `Línea ${b.linea}: ${b.mensaje}` : b.mensaje))));
  }
  const sinRespaldo = (r.pasos || []).filter((p) => p.forzada
    || Object.keys(p.indirectos || {}).length);
  if (sinRespaldo.length) {
    partes.push(h("p", {}, s.estado === "ejecutada"
      ? "Cambios sin respaldo, que Deshacer no revierte:"
      : "Cambios que el respaldo no cubre:"),
    h("ul", {}, sinRespaldo.map((p) => h("li", {}, `Línea ${p.linea}: ` +
      (p.forzada ? `${p.operacion} ejecutada sin respaldo`
        : Object.entries(p.indirectos).map(([tabla, valores]) =>
          `${sinEsquema(tabla)}, ${cambiosTexto(valores)}`).join("; ")) +
      "."))));
  }
  if (r.deshacer) {
    const d = r.deshacer;
    partes.push(h("p", {}, `Deshecha: ${d.pasos.map((p) =>
      `${numero(p.restauradas)} de ${numero(p.respaldadas)} filas en ` +
      `${p.tabla}`).join("; ")}.`));
    if (d.omitidas) partes.push(h("p", {}, `${numero(d.omitidas)} ` +
      `${filasTexto(d.omitidas)} se dejaron como estaban porque habían ` +
      "cambiado después de la ejecución."));
  }
  return h("div", { clase: `nota-bloque ${clase}` }, h("h3", {}, titulo),
    partes);
}

// Avisos que resumen todo el script: van abajo, no en el margen.
const AVISOS_DE_SCRIPT = new Set(["DML004", "DML005", "TX005"]);
// Repite lo que ya dicen los efectos de la propia anotación.
const AVISOS_REDUNDANTES = new Set(["SIM001"]);

function sinEsquema(tabla) {
  return tabla.replace(/^public\./, "");
}

function cambiosTexto([ins, upd, del]) {
  const plural = (n, palabra) => `${numero(n)} ${palabra}${n === 1 ? "" : "s"}`;
  return [ins && plural(ins, "insertada"), upd && plural(upd, "actualizada"),
    del && plural(del, "borrada")].filter(Boolean).join(", ");
}

function textoHallazgo(hallazgo) {
  if (hallazgo.codigo === "ESQ002") {
    const nombres = [...hallazgo.mensaje.matchAll(
      /([\w$]+): (?:BEFORE|AFTER|INSTEAD OF) /g)].map((m) => m[1]);
    return nombres.length === 1
      ? `Dispara el trigger ${nombres[0]}.`
      : `Dispara ${nombres.length} triggers: ${nombres.join(", ")}.`;
  }
  return hallazgo.mensaje;
}

// Junta, por línea de inicio de sentencia, lo que se anota en el margen.
// Devuelve { notas: línea → nodos, peso: línea → "riesgo" | "cambio" }.
// Solo se resaltan las sentencias que cambian datos o tienen avisos.
function anotacionesPorLinea(s, informe) {
  const notas = new Map();
  const peso = new Map();
  const marcar = (linea, valor) => {
    if (peso.get(linea) !== "riesgo") peso.set(linea, valor);
  };
  const anadir = (linea, nodo) => {
    if (!linea) return;
    if (!notas.has(linea)) notas.set(linea, []);
    notas.get(linea).push(nodo);
  };
  const ejecutada = s.estado === "ejecutada";
  const reales = new Map();
  for (const p of (s.resultado && s.resultado.pasos) || []) {
    reales.set(p.linea, p);
  }
  const impacto = informe.impacto;
  for (const r of (impacto && impacto.resultados) || []) {
    const real = ejecutada ? reales.get(r.linea) : null;
    const filas = real ? real.filas : r.filas;
    const etiqueta = real ? filasYParticipio(filas, "cambiada")
      : r.operacion === "SELECT" ? filasYParticipio(filas, "devuelta")
        : r.precision === "aproximado" ? `${filasTexto(filas)}, aproximado`
          : r.precision === "exacto" ? filasTexto(filas) : r.precision;
    const temporal = r.operacion.startsWith("CREATE TEMP");
    if (["UPDATE", "DELETE", "INSERT", "TRUNCATE", "MERGE"].includes(
      r.operacion) && filas !== 0) marcar(r.linea, "cambio");
    const efectos = Object.entries(r.efectos || {})
      .filter(([tabla]) => !r.tabla || sinEsquema(tabla) !== r.tabla)
      .map(([tabla, valores]) => h("span", { clase: "efecto" },
        `${sinEsquema(tabla)}: ${cambiosTexto(valores)} por trigger`));
    anadir(r.linea, h("div", { clase: "anotacion" },
      h("span", { clase: "filas" }, `${numero(filas)} `,
        h("small", {}, temporal ? `${filasTexto(filas)} en ${r.tabla}`
          : etiqueta)),
      r.nota && !["exacto"].includes(r.precision) && !temporal
        ? h("span", { clase: "efecto" }, r.nota) : null,
      efectos));
  }
  for (const hallazgo of informe.hallazgos || []) {
    if (!hallazgo.linea || AVISOS_DE_SCRIPT.has(hallazgo.codigo)
        || AVISOS_REDUNDANTES.has(hallazgo.codigo)) continue;
    if (hallazgo.nivel !== "info") {
      marcar(hallazgo.linea, ["crítico", "alto"].includes(hallazgo.nivel)
        ? "riesgo" : "cambio");
    }
    anadir(hallazgo.linea, h("div", { clase: "anotacion" },
      h("span", { clase: "hallazgo" }, etiquetaNivel(hallazgo.nivel),
        textoHallazgo(hallazgo))));
  }
  return { notas, peso };
}

// Líneas que ocupa cada sentencia: desde su inicio hasta antes de la
// siguiente, sin los comentarios y huecos que preceden a esa siguiente.
function rangosDeSentencias(informe, lineas) {
  const inicios = (informe.sentencias || []).map((x) => x.linea)
    .sort((a, b) => a - b);
  const rangos = new Map();
  inicios.forEach((inicio, i) => {
    let fin = i + 1 < inicios.length ? inicios[i + 1] - 1 : lineas.length;
    while (fin > inicio) {
      const texto = lineas[fin - 1].trim();
      if (texto && !texto.startsWith("--")) break;
      fin -= 1;
    }
    rangos.set(inicio, fin);
  });
  return rangos;
}

function scriptRevisado(s, informe) {
  const { notas, peso } = anotacionesPorLinea(s, informe);
  const lineas = s.sql.replace(/\r\n?/g, "\n").split("\n");
  if (lineas.length && lineas[lineas.length - 1] === "") lineas.pop();
  const rangos = rangosDeSentencias(informe, lineas);
  let ocupadaHasta = 0;
  let resaltadaHasta = 0;
  let pesoActual = null;
  const filas = lineas.map((texto, i) => {
    const n = i + 1;
    const comentario = texto.trimStart().startsWith("--");
    const celdas = [
      h("td", { clase: "n" }, String(n)),
      h("td", { clase: comentario ? "c comentario-sql" : "c" }, texto || " "),
    ];
    const anotaciones = notas.get(n);
    if (anotaciones) {
      const fin = Math.max(rangos.get(n) || n, n);
      pesoActual = peso.get(n) || null;
      celdas.push(h("td", { clase: pesoActual ? `m m-${pesoActual}` : "m",
        rowspan: String(fin - n + 1) }, anotaciones));
      ocupadaHasta = fin;
      resaltadaHasta = pesoActual ? fin : 0;
    } else if (n > ocupadaHasta) {
      celdas.push(h("td", { clase: "m" }));
    }
    const clases = [];
    if (n <= resaltadaHasta && pesoActual) {
      clases.push("anotada", `anotada-${pesoActual}`);
    }
    return h("tr", { clase: clases.join(" ") || null, id: `l${n}` },
      celdas);
  });
  const totalHallazgos = (informe.hallazgos || []).length;
  return h("section", { clase: "script-revisado",
    "aria-label": "Script con sus anotaciones" },
    h("div", { clase: "titulo-script" },
      h("span", {}, h("b", {}, s.script_nombre),
        ` ${lineas.length} líneas`),
      h("span", {}, `${totalHallazgos} ` +
        (totalHallazgos === 1 ? "aviso" : "avisos") +
        `, hash ${s.hash.slice(0, 12)}`)),
    h("div", { clase: "lineas" }, h("table", {}, h("tbody", {}, filas))));
}

function seccionHallazgosGenerales(informe) {
  const generales = (informe.hallazgos || []).filter((x) => !x.linea
    || AVISOS_DE_SCRIPT.has(x.codigo));
  if (!generales.length) return null;
  return h("section", {}, h("h2", {}, "Avisos del script completo"),
    h("ul", { clase: "lista-plana" }, generales.map((x) => h("li", {},
      etiquetaNivel(x.nivel), " ", x.mensaje))));
}

function seccionTriggers(informe) {
  const triggers = (informe.impacto && informe.impacto.triggers) || {};
  const tablas = Object.keys(triggers);
  if (!tablas.length) return null;
  return h("section", {}, h("h2", {}, "Triggers de las tablas modificadas"),
    h("ul", { clase: "lista-plana" }, tablas.map((tabla) => h("li", {},
      h("b", {}, tabla), h("ul", {}, triggers[tabla].map((t) =>
        h("li", { clase: "secundario pequeno" }, t)))))));
}

function seccionHistorial(s) {
  const comentario = h("textarea", { rows: "3",
    "aria-label": "Comentario", placeholder: "Escribe un comentario" });
  const enviar = botonAccion("Comentar", "", async () => {
    if (!comentario.value.trim()) return;
    const actualizada = await api("POST",
      `/api/solicitudes/${s.id}/comentar`, { comentario: comentario.value });
    pintarDetalle(actualizada);
  });
  return h("section", {}, h("h2", {}, "Historial"),
    h("ol", { clase: "historial" }, (s.eventos || []).map((e) => {
      const clase = EVENTOS_BUENOS.has(e.accion) ? "bueno"
        : EVENTOS_MALOS.has(e.accion) ? "malo" : null;
      const d = e.detalle || {};
      return h("li", { clase },
        h("span", { clase: "quien" }, e.usuario), " ",
        nombreEvento(e),
        d.cambios ? h("div", { clase: "secundario pequeno" },
          textoEdicion(d)) : null,
        h("div", { clase: "cuando" }, fecha(e.fecha)),
        d.comentario ? h("blockquote", {}, d.comentario) : null,
        d.motivo ? h("blockquote", {}, d.motivo) : null);
    })),
    h("div", { clase: "comentar" }, comentario,
      h("div", {}, enviar)));
}

// --- Auditoría ------------------------------------------------------------

const EVENTOS_DE_ACCESO = new Set(["acceso", "acceso_fallido"]);

async function verAuditoria() {
  const datos = await api("GET", "/api/auditoria");
  const verAccesos = h("input", { type: "checkbox" });
  const cuerpo = h("tbody");
  const pintarFilas = () => vaciar(cuerpo, datos.eventos
    .filter((e) => verAccesos.checked || !EVENTOS_DE_ACCESO.has(e.accion))
    .map(filaEvento));
  verAccesos.addEventListener("change", pintarFilas);
  const integridad = datos.intacta
    ? h("div", { clase: "integridad intacta" },
      h("b", {}, "Registro íntegro"),
      h("span", { clase: "secundario" },
        `Se han comprobado los ${numero(datos.eventos_verificados)} ` +
        "eventos: ninguno se ha modificado ni borrado."))
    : h("div", { clase: "integridad rota" },
      h("b", {}, "Registro alterado"),
      h("span", {}, `El evento ${datos.primer_evento_alterado} no coincide ` +
        "con la cadena de hashes: se ha modificado o borrado algo a mano."));
  vaciar(principal,
    h("div", { clase: "cabecera" }, h("div", {},
      h("h1", {}, "Auditoría"),
      h("p", {}, "Cada acción queda registrada con el hash de la anterior, " +
        "así que cualquier cambio posterior se detecta."))),
    integridad,
    h("label", { clase: "casilla opcion-tabla" }, verAccesos,
      h("span", {}, "Mostrar también los inicios de sesión")),
    h("div", { clase: "tabla-contenedor" },
      h("table", { clase: "tabla tabla-estatica" },
        h("thead", {}, h("tr", {},
          h("th", { scope: "col" }, "Fecha"),
          h("th", { scope: "col" }, "Usuario"),
          h("th", { scope: "col" }, "Acción"),
          h("th", { scope: "col" }, "Solicitud"),
          h("th", { clase: "ocultable", scope: "col" }, "Hash"))),
        cuerpo)));
  pintarFilas();
}

function filaEvento(e) {
  return h("tr", {},
    h("td", { clase: "secundario" }, fecha(e.fecha)),
    h("td", {}, e.usuario),
    h("td", {}, nombreEvento(e),
      e.detalle && e.detalle.usuario
        ? h("span", { clase: "secundario" }, ` (${e.detalle.usuario})`)
        : null),
    h("td", {}, e.solicitud_id
      ? h("a", { href: `#/solicitud/${e.solicitud_id}` },
        `Nº ${e.solicitud_id}`) : "—"),
    h("td", { clase: "ocultable hash" }, e.hash.slice(0, 16)));
}

// --- Usuarios -------------------------------------------------------------

async function verUsuarios() {
  if (estado.usuario.rol !== "admin") {
    location.hash = "#/";
    return;
  }
  const usuarios = await api("GET", "/api/usuarios");
  const nombre = h("input", { type: "text", required: true,
    pattern: "[A-Za-z0-9._\\-]+", autocomplete: "off" });
  const completo = h("input", { type: "text", autocomplete: "off" });
  const rol = h("select", {}, Object.entries(NOMBRES_ROL).map(
    ([valor, texto]) => h("option", { value: valor }, texto)));
  const clave = h("input", { type: "password", required: true,
    minlength: "10", autocomplete: "new-password" });
  const error = h("p", { clase: "error-formulario", hidden: true,
    role: "alert" });
  const formulario = h("form", { clase: "formulario" },
    h("h2", {}, "Añadir usuario"),
    h("div", { clase: "fila-campos" },
      h("label", { clase: "campo" }, h("span", {}, "Usuario"), nombre,
        h("small", {}, "Letras, números, punto, guion o guion bajo.")),
      h("label", { clase: "campo" }, h("span", {}, "Rol"), rol)),
    h("div", { clase: "fila-campos" },
      h("label", { clase: "campo" }, h("span", {}, "Nombre completo"),
        completo),
      h("label", { clase: "campo" }, h("span", {}, "Contraseña inicial"),
        clave, h("small", {}, "Mínimo 10 caracteres."))),
    error,
    h("div", { clase: "acciones-formulario" },
      h("button", { type: "submit", clase: "boton boton-principal" },
        "Añadir usuario")));
  formulario.addEventListener("submit", async (evento) => {
    evento.preventDefault();
    error.hidden = true;
    try {
      await api("POST", "/api/usuarios", {
        nombre: nombre.value, nombre_completo: completo.value,
        rol: rol.value, clave: clave.value,
      });
      avisar(`Usuario ${nombre.value} añadido.`);
      await verUsuarios();
    } catch (e) {
      if (e.codigo === 401) return;
      error.textContent = e.message;
      error.hidden = false;
    }
  });

  vaciar(principal,
    h("div", { clase: "cabecera" }, h("div", {},
      h("h1", {}, "Usuarios"),
      h("p", {}, "Los solicitantes envían scripts. Los aprobadores aprueban " +
        "los de otras personas, ejecutan y deshacen. Los administradores, " +
        "además, gestionan usuarios."))),
    h("div", { clase: "tabla-contenedor" },
      h("table", { clase: "tabla tabla-estatica" },
        h("thead", {}, h("tr", {},
          h("th", { scope: "col" }, "Usuario"),
          h("th", { scope: "col" }, "Nombre"),
          h("th", { scope: "col" }, "Rol"),
          h("th", { scope: "col" }, "Estado"),
          h("th", { scope: "col" },
            h("span", { clase: "solo-lector" }, "Acciones")))),
        h("tbody", {}, usuarios.map((u) => {
          const soyYo = u.id === estado.usuario.id;
          return h("tr", {},
            h("td", {}, h("b", {}, u.nombre),
              soyYo ? h("span", { clase: "secundario" }, " (tú)") : null),
            h("td", {}, u.nombre_completo || "—"),
            h("td", {}, h("span", { clase: "rol" }, NOMBRES_ROL[u.rol])),
            h("td", { clase: u.activo ? "" : "secundario" },
              u.activo ? "Activo" : "Desactivado"),
            h("td", {}, h("div", { clase: "acciones" },
              h("button", {
                type: "button", clase: "boton boton-texto",
                "aria-label": `Editar ${u.nombre}`,
                alClick: () => editarUsuario(u),
              }, "Editar"),
              soyYo ? null : botonAccion(
                u.activo ? "Desactivar" : "Activar", "boton-texto",
                async () => {
                  await api("POST", `/api/usuarios/${u.id}/activo`,
                    { activo: !u.activo });
                  await verUsuarios();
                }))));
        })))),
    h("div", { clase: "columnas" }, formulario));
}

// Diálogo para cambiar el nombre completo, el rol o la contraseña. El
// nombre de usuario no se cambia: la auditoría lo guarda como texto.
function editarUsuario(u) {
  const soyYo = u.id === estado.usuario.id;
  const completo = h("input", { type: "text", maxlength: "120",
    autocomplete: "off" });
  completo.value = u.nombre_completo;
  const rol = h("select", { disabled: soyYo }, Object.entries(NOMBRES_ROL)
    .map(([valor, texto]) => h("option", {
      value: valor, selected: valor === u.rol }, texto)));
  const clave = h("input", { type: "password", minlength: "10",
    autocomplete: "new-password" });
  const error = h("p", { clase: "error-formulario", hidden: true,
    role: "alert" });
  const guardar = h("button", { type: "submit",
    clase: "boton boton-principal" }, "Guardar cambios");
  const dialogo = h("dialog", { "aria-labelledby": "dialogo-titulo" });
  const formulario = h("form", { clase: "formulario" },
    h("div", {}, h("h2", { id: "dialogo-titulo" }, `Editar ${u.nombre}`),
      h("p", {}, "El nombre de usuario no se puede cambiar porque la " +
        "auditoría lo guarda tal cual.")),
    h("label", { clase: "campo" }, h("span", {}, "Nombre completo"),
      completo),
    h("label", { clase: "campo" }, h("span", {}, "Rol"), rol,
      soyYo ? h("small", {}, "No puedes quitarte a ti mismo el rol de " +
        "administrador.") : null),
    h("label", { clase: "campo" }, h("span", {}, "Contraseña nueva"), clave,
      h("small", {}, soyYo
        ? "Déjala vacía para no cambiarla. Mínimo 10 caracteres."
        : "Déjala vacía para no cambiarla. Mínimo 10 caracteres; al " +
          "cambiarla se cierran sus sesiones abiertas.")),
    error,
    h("div", { clase: "acciones" },
      h("button", { type: "button", clase: "boton",
        alClick: () => dialogo.close() }, "Volver"),
      guardar));
  formulario.addEventListener("submit", async (evento) => {
    evento.preventDefault();
    const cambios = {};
    if (completo.value.trim() !== u.nombre_completo) {
      cambios.nombre_completo = completo.value.trim();
    }
    if (!soyYo && rol.value !== u.rol) cambios.rol = rol.value;
    if (clave.value) cambios.clave = clave.value;
    if (!Object.keys(cambios).length) {
      dialogo.close();
      return;
    }
    guardar.disabled = true;
    error.hidden = true;
    try {
      await api("PATCH", `/api/usuarios/${u.id}`, cambios);
      dialogo.close();
      avisar(`Cambios guardados en ${u.nombre}.`);
      if (soyYo) {
        estado.usuario = await api("GET", "/api/yo");
        pintarCuenta();
      }
      await verUsuarios();
    } catch (e) {
      if (e.codigo === 401) { dialogo.close(); return; }
      error.textContent = e.message;
      error.hidden = false;
    } finally { guardar.disabled = false; }
  });
  dialogo.append(formulario);
  dialogo.addEventListener("close", () => dialogo.remove());
  document.body.append(dialogo);
  dialogo.showModal();
  completo.focus();
}

// --- Inicio ---------------------------------------------------------------

(async () => {
  try {
    estado.usuario = await api("GET", "/api/yo");
    await arrancar();
  } catch {
    mostrarAcceso();
  }
})();
