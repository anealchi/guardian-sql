"""Impacto contra PostgreSQL (usa la base de pruebas de conftest.py)."""

import psycopg
import pytest

from guardian.analisis import ALTO
from guardian.impacto import (
    APROXIMADO, ERROR, EXACTO, NO_SIMULADO, calcular,
)
from guardian.sentencias import leer_script

# Huella de todas las tablas de la base de pruebas: si cambia algo, cambia.
_SQL_HUELLA = """
select string_agg(t.tabla || ':' || t.huella, ',' order by t.tabla)
from (
    select 'parte_trabajo_tarea' as tabla,
        md5(string_agg(x::text, ',' order by x.id)) as huella
    from parte_trabajo_tarea x
    union all
    select 'orden_trabajo_tarea', md5(string_agg(x::text, ',' order by x.id))
    from orden_trabajo_tarea x
    union all
    select 'tarea_planificacion', md5(string_agg(x::text, ',' order by x.id))
    from tarea_planificacion x
    union all
    select 'parte_trabajo', md5(string_agg(x::text, ',' order by x.id))
    from parte_trabajo x
    union all
    select 'orden_trabajo', md5(string_agg(x::text, ',' order by x.id))
    from orden_trabajo x
    union all
    select 'log', count(*)::text from parte_trabajo_tarea_log
) t
"""


def huella(dsn):
    with psycopg.connect(dsn) as conexion:
        return conexion.execute(_SQL_HUELLA).fetchone()[0]


def por_linea(impacto):
    return {r.linea: r for r in impacto.resultados}


@pytest.mark.parametrize("modo", ["conteo", "simulacion"])
@pytest.mark.parametrize("nombre", [
    "borrar_tareas_duplicadas_parte.sql",
    "anular_ot_duplicadas_plan.sql",
    "borrar_plan_mantenimiento_duplicado.sql",
])
def test_ningun_modo_cambia_la_base(dsn, script_real, nombre, modo):
    antes = huella(dsn)
    impacto = calcular(dsn, leer_script(script_real(nombre)), modo=modo)
    assert huella(dsn) == antes
    assert not [r for r in impacto.resultados if r.precision == ERROR]


def test_conteo_de_borrar_tareas_duplicadas(dsn, script_real):
    sentencias = leer_script(script_real("borrar_tareas_duplicadas_parte.sql"))
    resultados = por_linea(calcular(dsn, sentencias))
    # Tablas temporales: 4 tareas en grupos duplicados, 2 a borrar.
    assert resultados[25].filas == 4
    assert resultados[61].filas == 2
    # UPDATE de parte_trabajo_tarea: exacto.
    assert (resultados[89].filas, resultados[89].precision) == (2, EXACTO)
    # Los dos siguientes dependen del UPDATE anterior: aproximados.
    assert resultados[102].precision == APROXIMADO
    assert resultados[124].precision == APROXIMADO


def test_simulacion_da_numeros_exactos_y_efectos_de_triggers(
        dsn, script_real):
    sentencias = leer_script(script_real("borrar_tareas_duplicadas_parte.sql"))
    impacto = calcular(dsn, sentencias, modo="simulacion")
    resultados = por_linea(impacto)
    assert [resultados[n].filas for n in (89, 102, 124)] == [2, 2, 2]
    efectos = resultados[89].efectos
    assert efectos["public.parte_trabajo_tarea"] == (0, 2, 0)
    assert efectos["public.parte_trabajo_tarea_log"] == (2, 0, 0)
    # La comprobación final no devuelve duplicados.
    assert resultados[145].filas == 0
    assert any(h.codigo == "SIM001" for h in impacto.hallazgos)


def test_trigger_en_cascada_de_anular_ot(dsn, script_real):
    sentencias = leer_script(script_real("anular_ot_duplicadas_plan.sql"))
    impacto = calcular(dsn, sentencias, modo="simulacion")
    resultados = por_linea(impacto)
    # Anular la tarea del parte (tarea_estado = 90) dispara
    # update_tarea_estado_parte, que anula la tarea de la OT.
    assert resultados[163].efectos["public.orden_trabajo_tarea"] == (0, 1, 0)
    # Por eso el UPDATE explícito de orden_trabajo_tarea ya no toca nada.
    assert resultados[193].filas == 0
    assert resultados[223].filas == 1
    disparados = [h for h in impacto.hallazgos if h.codigo == "ESQ002"]
    assert "update_tarea_estado_parte" in disparados[0].mensaje


def test_trigger_update_of_solo_salta_con_sus_columnas(dsn):
    sql = ("begin; update parte_trabajo_tarea set estado = false "
           "where id = 1; rollback;")
    impacto = calcular(dsn, leer_script(sql))
    mensaje = next(h.mensaje for h in impacto.hallazgos
                   if h.codigo == "ESQ002")
    assert "add_parte_trabajo_tarea_log" in mensaje
    assert "update_tarea_estado_parte" not in mensaje


def test_tabla_inexistente(dsn):
    sql = "begin; update no_existe set a = 1 where id = 1; rollback;"
    impacto = calcular(dsn, leer_script(sql))
    assert any(h.codigo == "ESQ001" and h.nivel == ALTO
               for h in impacto.hallazgos)
    assert impacto.resultados[0].precision == ERROR


def test_cascada_de_claves_foraneas(dsn):
    sql = "begin; delete from parte_trabajo where id = 1; rollback;"
    impacto = calcular(dsn, leer_script(sql), modo="simulacion")
    assert any(h.codigo == "ESQ003" for h in impacto.hallazgos)
    efectos = impacto.resultados[0].efectos
    assert efectos["public.parte_trabajo_tarea"][2] == 5


def test_un_error_no_detiene_el_conteo(dsn):
    sql = ("begin; update no_existe set a = 1 where id = 1;\n"
           "update parte_trabajo_tarea set estado = false where id = 1;\n"
           "rollback;")
    resultados = calcular(dsn, leer_script(sql)).resultados
    assert [r.precision for r in resultados] == [ERROR, EXACTO]
    assert resultados[1].filas == 1


def test_simulacion_se_niega_con_sentencias_fuera_de_transaccion(dsn):
    sql = "create index concurrently i on parte_trabajo (id);"
    with pytest.raises(ValueError):
        calcular(dsn, leer_script(sql), modo="simulacion")


@pytest.mark.parametrize("sql", [
    "do $$ begin update parte_trabajo_tarea set estado = false; end $$;",
    "copy parte_trabajo_tarea (id, parte_trabajo_id) from stdin;",
])
def test_conteo_no_ejecuta_sentencias_sin_tablas_conocidas(dsn, sql):
    resultados = calcular(dsn, leer_script(sql)).resultados
    assert [r.precision for r in resultados] == [NO_SIMULADO]
