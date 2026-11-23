"""Ejecución con respaldo y deshacer (usa la base de pruebas)."""

import psycopg
import pytest

from guardian.cli import main
from guardian.deshacer import ErrorDeshacer, deshacer, script_deshacer
from guardian.ejecucion import ErrorEjecucion, ejecutar
from guardian.sentencias import leer_script

LOG = ["parte_trabajo_tarea_log"]

# Huella de las tablas de negocio (sin el log, que crece también al
# deshacer porque el trigger salta en los dos sentidos).
_SQL_HUELLA = """
select md5(string_agg(t.h, ',' order by t.n)) from (
    select 1 as n, string_agg(x::text, ',' order by x.id) as h
    from parte_trabajo_tarea x
    union all select 2, string_agg(x::text, ',' order by x.id)
    from orden_trabajo_tarea x
    union all select 3, string_agg(x::text, ',' order by x.id)
    from tarea_planificacion x
    union all select 4, string_agg(x::text, ',' order by x.id)
    from parte_trabajo x
    union all select 5, string_agg(x::text, ',' order by x.id)
    from orden_trabajo x
    union all select 6, string_agg(x::text, ',' order by x.id)
    from tarea_plan_mantenimiento x
) t
"""


def huella(dsn):
    with psycopg.connect(dsn) as conexion:
        return conexion.execute(_SQL_HUELLA).fetchone()[0]


def consultar(dsn, sql):
    with psycopg.connect(dsn) as conexion:
        return conexion.execute(sql).fetchall()


def hay_respaldo(dsn):
    return consultar(dsn, "select to_regclass('guardian_backup."
                          "ejecuciones') is not null")[0][0]


def aplicar(dsn, sql, nombre="prueba.sql", **opciones):
    opciones.setdefault("ignorar", LOG)
    return ejecutar(dsn, nombre, sql, leer_script(sql), lambda _: True,
                    **opciones)


def si(_):
    return True


def test_ida_y_vuelta_con_script_real(dsn, script_real):
    fuente = script_real("borrar_tareas_duplicadas_parte.sql")
    original = huella(dsn)
    ejecucion = aplicar(dsn, fuente)
    assert ejecucion.aplicada
    assert ejecucion.filas_respaldadas == 6
    assert huella(dsn) != original
    assert consultar(dsn, "select count(*) from parte_trabajo_tarea "
                          "where estado is false") == [(2,)]

    resultado = deshacer(dsn, ejecucion.id, si)
    assert resultado.aplicado and resultado.omitidas == 0
    assert huella(dsn) == original
    assert consultar(dsn, "select estado from guardian_backup.ejecuciones"
                          f" where id = '{ejecucion.id}'") == [("deshecho",)]


def test_sin_confirmar_no_queda_nada(dsn, script_real):
    fuente = script_real("borrar_tareas_duplicadas_parte.sql")
    original = huella(dsn)
    ejecucion = ejecutar(dsn, "x.sql", fuente, leer_script(fuente),
                         lambda _: False, ignorar=LOG)
    assert not ejecucion.aplicada
    assert ejecucion.filas_respaldadas == 6
    assert huella(dsn) == original
    assert not hay_respaldo(dsn)


def test_cambios_de_triggers_sin_respaldo_impiden_confirmar(
        dsn, script_real):
    fuente = script_real("anular_ot_duplicadas_plan.sql")
    original = huella(dsn)
    llamado = []
    ejecucion = ejecutar(dsn, "x.sql", fuente, leer_script(fuente),
                         lambda e: llamado.append(e) or True, ignorar=LOG)
    assert not ejecucion.aplicada and not llamado
    paso = next(p for p in ejecucion.pasos if p.linea == 163)
    assert paso.indirectos == {"public.orden_trabajo_tarea": (0, 1, 0)}
    assert paso.ignorados == {"public.parte_trabajo_tarea_log": (1, 0, 0)}
    assert huella(dsn) == original

    permitida = aplicar(dsn, fuente, permitir_indirectos=True)
    assert permitida.aplicada
    sin_respaldo = consultar(dsn, "select sin_respaldo from guardian_backup"
                                  f".ejecuciones where id = '{permitida.id}'")
    assert sin_respaldo[0][0][0]["linea"] == 163


def test_insert_y_delete_se_deshacen(dsn):
    sql = """
        begin;
        insert into tarea_plan_mantenimiento (id, codigo)
            values (50, 'A'), (51, 'B');
        with nuevos as (select 52 as id, 'C' as codigo)
        insert into tarea_plan_mantenimiento as tpm (id, codigo)
            select id, codigo from nuevos returning tpm.id;
        delete from parte_trabajo_tarea where id in (3, 6);
        commit;
    """
    original = huella(dsn)
    ejecucion = aplicar(dsn, sql)
    assert ejecucion.aplicada
    assert [p.respaldadas for p in ejecucion.pasos] == [2, 1, 2]
    # 3 filas iniciales + 3 insertadas.
    assert consultar(dsn, "select count(*) from tarea_plan_mantenimiento") \
        == [(6,)]
    assert deshacer(dsn, ejecucion.id, si).aplicado
    assert huella(dsn) == original


def test_filas_cambiadas_despues_se_omiten(dsn, script_real):
    ejecucion = aplicar(dsn, script_real(
        "borrar_tareas_duplicadas_parte.sql"))
    with psycopg.connect(dsn, autocommit=True) as conexion:
        conexion.execute("update orden_trabajo_tarea set estado = true "
                         "where id = 2")
    resultado = deshacer(dsn, ejecucion.id, si)
    assert resultado.omitidas == 1
    # La fila cambiada por otra sesión conserva ese cambio.
    assert consultar(dsn, "select estado, deleted_by from "
                          "orden_trabajo_tarea where id = 2") == [(True, 49)]
    assert consultar(dsn, "select estado, deleted_by from "
                          "orden_trabajo_tarea where id = 4") == [(True, None)]


def test_no_se_deshace_dos_veces(dsn, script_real):
    ejecucion = aplicar(dsn, script_real(
        "borrar_tareas_duplicadas_parte.sql"))
    deshacer(dsn, ejecucion.id, si)
    with pytest.raises(ErrorDeshacer):
        deshacer(dsn, ejecucion.id, si)


def test_sql_para_deshacer_a_mano(dsn, script_real):
    ejecucion = aplicar(dsn, script_real(
        "borrar_tareas_duplicadas_parte.sql"))
    sql = script_deshacer(dsn, ejecucion.id)
    assert sql.startswith("-- Deshacer") and sql.rstrip().endswith(
        "ROLLBACK;")
    # Ejecutado tal cual (con ROLLBACK) no cambia nada.
    antes = huella(dsn)
    with psycopg.connect(dsn, autocommit=True) as conexion:
        conexion.execute(sql)
    assert huella(dsn) == antes


@pytest.mark.parametrize("sql, texto", [
    ("begin; create table sin_clave (a int); "
     "update sin_clave set a = 1 where a = 2; rollback;", "CREATE TABLE"),
    ("begin; truncate parte_trabajo_tarea_log; rollback;", "TRUNCATE"),
    ("begin; alter table orden_trabajo add column x int; rollback;",
     "ALTER TABLE"),
    ("begin; do $$ begin perform 1; end $$; rollback;", "DO"),
    ("begin; update parte_trabajo_tarea set id = 99 where id = 1; "
     "rollback;", "clave primaria"),
])
def test_sentencias_sin_respaldo_posible_se_bloquean(dsn, sql, texto):
    original = huella(dsn)
    ejecucion = aplicar(dsn, sql)
    assert not ejecucion.aplicada and not ejecucion.pasos
    assert any(texto in b.mensaje for b in ejecucion.bloqueos)
    assert huella(dsn) == original


def test_forzar_ejecuta_y_marca_sin_respaldo(dsn):
    sql = "begin; truncate parte_trabajo_tarea_log; rollback;"
    with psycopg.connect(dsn, autocommit=True) as conexion:
        conexion.execute("insert into parte_trabajo_tarea_log "
                         "(parte_trabajo_tarea_id) values (1)")
    ejecucion = ejecutar(dsn, "x.sql", sql, leer_script(sql), si,
                         forzar=True)
    assert not ejecucion.aplicada  # el truncate no tiene respaldo
    assert ejecucion.sin_respaldo
    ejecucion = ejecutar(dsn, "x.sql", sql, leer_script(sql), si,
                         forzar=True, permitir_indirectos=True)
    assert ejecucion.aplicada
    assert consultar(dsn, "select count(*) from parte_trabajo_tarea_log") \
        == [(0,)]


def test_drop_de_temporal_no_borra_una_permanente_con_el_mismo_nombre(dsn):
    with psycopg.connect(dsn, autocommit=True) as conexion:
        conexion.execute("create table tmp_a_borrar (id int)")
    sql = """
        begin;
        drop table if exists tmp_a_borrar;
        create temp table tmp_a_borrar as select 2 as id;
        update parte_trabajo_tarea set estado = false
        where id in (select id from tmp_a_borrar);
        rollback;
    """
    assert aplicar(dsn, sql).aplicada
    assert consultar(dsn, "select to_regclass('public.tmp_a_borrar') "
                          "is not null") == [(True,)]


def test_un_error_deshace_todo(dsn):
    sql = ("begin; update parte_trabajo_tarea set estado = false "
           "where id = 1; update no_existe set a = 1 where id = 1; commit;")
    original = huella(dsn)
    with pytest.raises(ErrorEjecucion) as error:
        ejecutar(dsn, "x.sql", sql, leer_script(sql), si, forzar=True,
                 permitir_indirectos=True, ignorar=LOG)
    assert error.value.linea == 1
    assert huella(dsn) == original
    assert not hay_respaldo(dsn)


def test_cli_ejecutar_historial_y_deshacer(dsn, script_real, tmp_path,
                                           capsys):
    script = tmp_path / "borrar.sql"
    script.write_text(script_real("borrar_tareas_duplicadas_parte.sql"),
                      encoding="utf-8")
    original = huella(dsn)
    salida = main(["ejecutar", str(script), "--dsn", dsn, "--confirmar",
                   "--ignorar-tabla", "parte_trabajo_tarea_log"])
    texto = capsys.readouterr().out
    assert salida == 0 and "Aplicado" in texto
    ejecucion_id = texto.split("guardian deshacer ")[1].split()[0]

    assert main(["historial", "--dsn", dsn]) == 0
    assert ejecucion_id in capsys.readouterr().out

    assert main(["deshacer", ejecucion_id, "--dsn", dsn,
                 "--confirmar"]) == 0
    assert huella(dsn) == original


def test_cli_sin_conexion(capsys, monkeypatch):
    monkeypatch.delenv("GUARDIAN_DSN", raising=False)
    assert main(["historial"]) == 2
    assert "necesita una conexión" in capsys.readouterr().err


def test_dos_cambios_en_la_misma_fila_vuelven_al_valor_original(dsn):
    sql = """
        begin;
        update orden_trabajo set numero_ot = 'OT-1-b' where id = 1;
        update orden_trabajo set numero_ot = 'OT-1-c',
            clasificacion_orden_trabajo_estado = 20 where id = 1;
        rollback;
    """
    original = huella(dsn)
    ejecucion = aplicar(dsn, sql)
    assert consultar(dsn, "select numero_ot from orden_trabajo "
                          "where id = 1") == [("OT-1-c",)]
    resultado = deshacer(dsn, ejecucion.id, si)
    assert resultado.omitidas == 0
    assert huella(dsn) == original
