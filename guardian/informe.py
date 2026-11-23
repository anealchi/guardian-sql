"""Presentación de los resultados: texto para la consola y Markdown."""

from guardian.analisis import ALTO, CRITICO, NIVELES
from guardian.impacto import APROXIMADO, ERROR, EXACTO
from guardian.sentencias import tablas_permanentes_escritas

_ICONOS = {"crítico": "[!!]", "alto": "[! ]", "medio": "[~ ]",
           "info": "[i ]"}
_MODOS = {"conteo": "conteo (sin escribir nada)",
          "simulacion": "simulación (ejecutado y deshecho con ROLLBACK)"}


def veredicto(hallazgos):
    """Resumen en una línea según el hallazgo más grave."""
    niveles = {h.nivel for h in hallazgos}
    if CRITICO in niveles:
        return ("BLOQUEADO: hay riesgos críticos que corregir antes de "
                "ejecutar.")
    if ALTO in niveles:
        return "REVISAR: hay riesgos altos que alguien debe aprobar."
    return "OK: sin riesgos altos detectados."


def _filas(resultado):
    if resultado.filas is None:
        return "—"
    texto = f"{resultado.filas:,}".replace(",", ".")
    return f"~{texto}" if resultado.precision == APROXIMADO else texto


def _efectos(resultado):
    partes = []
    for tabla, (ins, upd, dele) in sorted(resultado.efectos.items()):
        cambios = [f"{n} {etiqueta}{'' if n == 1 else 's'}"
                   for n, etiqueta in ((ins, "insertada"),
                                       (upd, "actualizada"),
                                       (dele, "borrada")) if n]
        partes.append(f"{tabla}: {', '.join(cambios)}")
    return "; ".join(partes)


def _contar_niveles(hallazgos):
    return ", ".join(
        f"{sum(h.nivel == n for h in hallazgos)} {n}" for n in NIVELES)


def texto_consola(nombre, sentencias, hallazgos, impacto=None):
    lineas = [f"SQL Guardian — {nombre}", ""]
    escritas = tablas_permanentes_escritas(sentencias)
    lineas.append(f"Sentencias: {len(sentencias)}. Tablas que modifica: "
                  f"{', '.join(escritas) or 'ninguna'}.")
    lineas.append(f"Hallazgos: {_contar_niveles(hallazgos)}.")
    lineas.append("")
    for h in hallazgos:
        donde = f"línea {h.linea}" if h.linea else "script"
        lineas.append(f"{_ICONOS[h.nivel]} {h.codigo} ({donde}) {h.mensaje}")

    if impacto is not None:
        lineas += ["", f"Impacto en PostgreSQL {impacto.servidor}, modo "
                   f"{_MODOS.get(impacto.modo, impacto.modo)}:"]
        if not impacto.resultados:
            lineas.append("  (ninguna sentencia que medir)")
        for r in impacto.resultados:
            objetivo = f" {r.tabla}" if r.tabla else ""
            unidad = "fila" if r.filas == 1 else "filas"
            lineas.append(f"  línea {r.linea:>4}  {r.operacion}{objetivo}: "
                          f"{_filas(r)} {unidad}")
            if r.precision == ERROR:
                lineas.append(f"              ERROR: {r.nota}")
            elif r.nota and r.precision != EXACTO:
                lineas.append(f"              {r.nota}")
            if r.efectos:
                lineas.append(f"              cambios reales: "
                              f"{_efectos(r)}")
    lineas += ["", veredicto(hallazgos)]
    return "\n".join(lineas)


def _celda(texto):
    return str(texto).replace("|", "\\|").replace("\n", " ")


def markdown(nombre, sentencias, hallazgos, impacto=None):
    escritas = tablas_permanentes_escritas(sentencias)
    partes = [
        f"# Informe de SQL Guardian: `{nombre}`",
        "",
        f"**Veredicto:** {veredicto(hallazgos)}",
        "",
        f"- Sentencias: {len(sentencias)}",
        f"- Tablas que modifica: "
        f"{', '.join(f'`{t}`' for t in escritas) or 'ninguna'}",
        f"- Hallazgos: {_contar_niveles(hallazgos)}",
        "",
        "## Hallazgos",
        "",
    ]
    if hallazgos:
        partes += ["| Nivel | Código | Línea | Detalle |",
                   "|-------|--------|-------|---------|"]
        partes += [f"| {h.nivel} | {h.codigo} | {h.linea or '—'} | "
                   f"{_celda(h.mensaje)} |" for h in hallazgos]
    else:
        partes.append("Ninguno.")

    if impacto is not None:
        partes += ["", "## Impacto", "",
                   f"PostgreSQL {impacto.servidor}, modo "
                   f"{_MODOS.get(impacto.modo, impacto.modo)}.", ""]
        if impacto.resultados:
            partes += ["| Línea | Operación | Tabla | Filas | Precisión "
                       "| Notas |",
                       "|-------|-----------|-------|-------|-----------"
                       "|-------|"]
            for r in impacto.resultados:
                notas = " ".join(x for x in (r.nota, _efectos(r)) if x)
                partes.append(
                    f"| {r.linea} | {r.operacion} | {r.tabla or '—'} | "
                    f"{_filas(r)} | {r.precision} | {_celda(notas)} |")
        else:
            partes.append("Ninguna sentencia que medir.")

        con_triggers = [t for t in impacto.tablas.values() if t.triggers]
        if con_triggers:
            partes += ["", "## Triggers de las tablas modificadas", ""]
            for tabla in con_triggers:
                partes.append(f"- `{tabla.nombre}`")
                partes += [f"  - {t.describir()}"
                           + ("" if t.activo else " (desactivado)")
                           for t in tabla.triggers]

    partes += ["", "## Sentencias", ""]
    partes += [f"{s.indice}. Línea {s.linea}: `{_celda(s.resumen)}`"
               for s in sentencias]
    return "\n".join(partes) + "\n"


def _cambios(efectos):
    partes = []
    for tabla, (ins, upd, dele) in sorted(efectos.items()):
        cambios = [f"{n} {etiqueta}{'' if n == 1 else 's'}"
                   for n, etiqueta in ((ins, "insertada"),
                                       (upd, "actualizada"),
                                       (dele, "borrada")) if n]
        partes.append(f"{tabla}: {', '.join(cambios)}")
    return "; ".join(partes)


def texto_ejecucion(ejecucion):
    """Resumen de `guardian ejecutar` antes de pedir la confirmación."""
    lineas = [f"SQL Guardian — ejecución {ejecucion.id} "
              f"({ejecucion.script})", ""]
    if ejecucion.bloqueos:
        lineas.append("Sentencias sin respaldo posible:")
        lineas += [f"  línea {b.linea:>4}  {b.mensaje}"
                   for b in ejecucion.bloqueos]
        lineas.append("")
    for p in ejecucion.pasos:
        objetivo = f" {p.tabla}" if p.tabla else ""
        filas = "—" if p.filas is None else p.filas
        unidad = "fila" if p.filas == 1 else "filas"
        respaldo = (f" (respaldadas: {p.respaldadas})"
                    if p.respaldadas is not None else "")
        lineas.append(f"  línea {p.linea:>4}  {p.operacion}{objetivo}: "
                      f"{filas} {unidad}{respaldo}")
        if p.forzada:
            lineas.append(f"              {p.nota}")
        if p.indirectos:
            lineas.append(f"              SIN RESPALDO: "
                          f"{_cambios(p.indirectos)}")
        if p.ignorados:
            lineas.append(f"              ignorado: {_cambios(p.ignorados)}")
    if ejecucion.pasos:
        lineas += ["", f"Filas respaldadas: {ejecucion.filas_respaldadas}."]
        if ejecucion.sin_respaldo:
            lineas.append(f"Atención: {len(ejecucion.sin_respaldo)} "
                          "sentencia(s) con cambios que `guardian deshacer` "
                          "no podrá revertir.")
    return "\n".join(lineas)


def texto_deshacer(resultado):
    lineas = [f"SQL Guardian — deshacer {resultado.id} ({resultado.script}, "
              f"{resultado.usuario}, {resultado.fecha})", ""]
    for p in resultado.pasos:
        restauradas = "—" if p.restauradas is None else p.restauradas
        lineas.append(f"  línea {p.linea:>4}  {p.operacion} {p.tabla}: "
                      f"{restauradas} de {p.respaldadas} filas restauradas")
        if p.omitidas:
            lineas.append(f"              {p.omitidas} omitidas: han "
                          "cambiado (o ya no existen) desde la ejecución.")
        if p.indirectos:
            lineas.append(f"              cambios por triggers: "
                          f"{_cambios(p.indirectos)}")
    if resultado.sin_respaldo:
        lineas += ["", "No se pueden deshacer (no tenían respaldo):"]
        for item in resultado.sin_respaldo:
            cambios = {t: tuple(v) for t, v in item["cambios"].items()}
            detalle = _cambios(cambios) or "ejecutada con --forzar"
            lineas.append(f"  línea {item['linea']:>4}  "
                          f"{item['operacion']} {item.get('tabla') or ''}: "
                          f"{detalle}")
    if resultado.omitidas:
        lineas += ["", f"Atención: {resultado.omitidas} filas no se "
                   "restauran porque han cambiado desde la ejecución."]
    return "\n".join(lineas)


def texto_historial(ejecuciones):
    if not ejecuciones:
        return "No hay ejecuciones registradas."
    lineas = [f"{'Ejecución':<21} {'Fecha':<19} {'Usuario':<12} "
              f"{'Estado':<9} {'Filas':>6}  Script"]
    for e in ejecuciones:
        aviso = " (con cambios sin respaldo)" if e["sin_respaldo"] else ""
        lineas.append(
            f"{e['id']:<21} {e['fecha']:%Y-%m-%d %H:%M:%S} "
            f"{e['usuario'][:12]:<12} {e['estado']:<9} {e['filas']:>6}  "
            f"{e['script']}{aviso}")
    return "\n".join(lineas)
