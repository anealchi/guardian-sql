"""Reglas del análisis estático (no necesitan base de datos)."""

import pytest

from guardian.analisis import ALTO, CRITICO, INFO, MEDIO, analizar
from guardian.impacto import consulta_de_conteo
from guardian.sentencias import ErrorSintaxis, leer_script


def codigos(sql):
    return [h.codigo for h in analizar(leer_script(sql))]


def hallazgo(sql, codigo):
    return next(h for h in analizar(leer_script(sql)) if h.codigo == codigo)


SEGURO = "begin;\nupdate t set a = 1 where id = 2;\nrollback;\n"


def test_script_seguro_no_tiene_avisos_graves():
    niveles = {h.nivel for h in analizar(leer_script(SEGURO))}
    assert not niveles & {CRITICO, ALTO, MEDIO}


def test_lineas_y_texto_sin_comentarios_previos():
    sql = "-- cabecera\n-- otra\nbegin;\n\n-- paso 1\nupdate t set a = 1 " \
          "where id = 2;\nrollback;"
    sentencias = leer_script(sql)
    assert [s.linea for s in sentencias] == [3, 6, 7]
    assert sentencias[1].texto.startswith("update t")
    assert sentencias[1].columnas == ["a"]


def test_error_de_sintaxis_indica_la_linea():
    with pytest.raises(ErrorSintaxis) as error:
        leer_script("begin;\nupdat t set a = 1;\n")
    assert error.value.linea == 2


@pytest.mark.parametrize("sql", [
    "begin; update t set a = 1; rollback;",
    "begin; delete from t; rollback;",
])
def test_dml_sin_where_es_critico(sql):
    h = hallazgo(sql, "DML001")
    assert h.nivel == CRITICO


def test_dml_sin_where_en_temporal_no_avisa():
    sql = ("begin; create temp table x as select 1 as a; "
           "update x set a = 2; rollback;")
    assert "DML001" not in codigos(sql)


def test_sin_begin():
    assert "TX001" in codigos("update t set a = 1 where id = 1;")
    assert "TX001" not in codigos(SEGURO)


def test_set_local_antes_de_begin_no_cuenta_como_primera():
    sql = "set search_path = public; begin; update t set a = 1 " \
          "where id = 1; rollback;"
    assert "TX001" not in codigos(sql)


def test_termina_en_commit():
    sql = "begin; update t set a = 1 where id = 1; commit;"
    assert hallazgo(sql, "TX002").nivel == ALTO


def test_commit_intermedio_y_varias_transacciones():
    sql = ("begin; update t set a = 1 where id = 1; commit;\n"
           "begin; update t set a = 2 where id = 2; rollback;")
    resultado = codigos(sql)
    assert "TX004" in resultado
    assert "TX005" in resultado
    assert "TX002" not in resultado


def test_cambios_despues_del_ultimo_cierre():
    sql = "begin; select 1; rollback; update t set a = 1 where id = 1;"
    assert "TX003" in codigos(sql)


def test_drop_de_temporal_tras_commit_no_es_cambio_pendiente():
    sql = ("begin; create temp table x as select 1; "
           "update t set a = 1 where id = 1; commit; drop table x;")
    assert "TX003" not in codigos(sql)


def test_drop_de_tabla_permanente_es_critico():
    assert hallazgo("begin; drop table clientes; rollback;",
                    "DDL001").nivel == CRITICO


def test_drop_de_temporal_sin_esquema_avisa_del_riesgo():
    sql = ("begin; drop table if exists tmp_x; "
           "create temp table tmp_x as select 1; rollback;")
    resultado = codigos(sql)
    assert "DDL002" in resultado
    assert "DDL001" not in resultado


def test_drop_de_temporal_con_pg_temp_no_avisa():
    sql = ("begin; drop table if exists pg_temp.tmp_x; "
           "create temp table tmp_x as select 1; rollback;")
    assert not {"DDL001", "DDL002"} & set(codigos(sql))


def test_truncate():
    assert hallazgo("begin; truncate clientes; rollback;",
                    "DML003").nivel == CRITICO


def test_alter_table_bloqueante_y_ligero():
    assert "DDL003" in codigos(
        "begin; alter table t add column b int; rollback;")
    assert "DDL003" not in codigos(
        "begin; alter table t alter column b set statistics 100; rollback;")


def test_create_index_sin_concurrently():
    assert "DDL004" in codigos("begin; create index i on t (a); rollback;")
    assert "DDL004" not in codigos("create index concurrently i on t (a);")


def test_borrado_fisico():
    assert hallazgo("begin; delete from t where id = 1; rollback;",
                    "DML002").nivel == MEDIO


def test_borrado_logico_y_usuario_fijo_se_agrupan():
    sql = ("begin;\n"
           "update a set estado = false, deleted_by = 49 where id = 1;\n"
           "update b set estado = false, deleted_by = 49 where id = 1;\n"
           "rollback;")
    hallazgos = analizar(leer_script(sql))
    borrado = [h for h in hallazgos if h.codigo == "DML004"]
    usuario = [h for h in hallazgos if h.codigo == "DML005"]
    assert len(borrado) == 1 and "a, b" in borrado[0].mensaje
    assert len(usuario) == 1 and "49" in usuario[0].mensaje
    assert borrado[0].nivel == INFO


def test_fechas_fijas():
    h = hallazgo("begin; update t set a = 1 where f >= '2026-11-16'; "
                 "rollback;", "GEN001")
    assert "2026-11-16" in h.mensaje


def test_modificacion_escondida_en_with():
    sql = ("begin; with x as (delete from t where id = 1 returning id) "
           "select * from x; rollback;")
    assert hallazgo(sql, "GEN002").nivel == ALTO


def test_conteo_de_update_simple():
    nodo = leer_script("update t set a = 1 where b = 2")[0].nodo
    assert consulta_de_conteo(nodo) == "SELECT count(*) FROM t WHERE b = 2"


def test_conteo_de_update_con_from_cuenta_filas_distintas():
    nodo = leer_script("update t x set a = 1 from u where u.id = x.id")[0]
    sql = consulta_de_conteo(nodo.nodo)
    assert sql == ("SELECT count(DISTINCT (x.tableoid, x.ctid)) "
                   "FROM t AS x, u WHERE u.id = x.id")


def test_conteo_de_delete_con_using_y_with():
    nodo = leer_script("with v as (select 1 as id) delete from t "
                       "using v where v.id = t.id")[0].nodo
    sql = consulta_de_conteo(nodo)
    assert sql.startswith("WITH v AS (SELECT 1 AS id) SELECT count(DISTINCT")
    assert sql.endswith("FROM t, v WHERE v.id = t.id")


def test_conteo_de_insert():
    nodo = leer_script("insert into t (a) values (1), (2), (3)")[0].nodo
    assert consulta_de_conteo(nodo) == "SELECT 3"
    nodo = leer_script("insert into t (a) select b from u")[0].nodo
    assert consulta_de_conteo(nodo) == (
        "SELECT count(*) FROM (SELECT b FROM u) AS guardian_origen")
