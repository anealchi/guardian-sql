-- Esquema mínimo de mantenimiento para las pruebas, con datos inventados.
-- Reproduce las tablas y columnas que usan los scripts de
-- operaciones-sql-produccion, más dos triggers:
--   * add_parte_trabajo_tarea_log: cualquier UPDATE de parte_trabajo_tarea
--     inserta una fila en parte_trabajo_tarea_log.
--   * update_tarea_estado_parte: solo con UPDATE OF tarea_estado; anula la
--     tarea de OT asociada.

create table tarea_plan_mantenimiento (
    id integer primary key,
    codigo text not null
);

create table orden_trabajo (
    id integer primary key,
    numero_ot text not null,
    clasificacion_orden_trabajo_estado integer not null default 10,
    fecha_anulacion timestamp,
    estado boolean not null default true,
    created_at timestamp not null default now(),
    updated_at timestamp,
    updated_by integer,
    deleted_at timestamp,
    deleted_by integer
);

create table tarea_planificacion (
    id integer primary key,
    codigo text not null,
    empresa_id integer not null default 1,
    tarea_plan_mantenimiento_id integer references tarea_plan_mantenimiento,
    fecha_proxima_ejecucion timestamp not null,
    empresa_ubicacion_id integer,
    empresa_activo_id integer,
    estado_tarea integer not null default 10,
    estado boolean not null default true,
    created_at timestamp not null default now(),
    updated_at timestamp,
    updated_by integer,
    deleted_at timestamp,
    deleted_by integer
);

create table orden_trabajo_tarea (
    id integer primary key,
    orden_trabajo_id integer not null references orden_trabajo,
    tarea_planificacion_id integer not null references tarea_planificacion,
    tarea_estado integer not null default 10,
    estado boolean not null default true,
    updated_at timestamp,
    updated_by integer,
    deleted_at timestamp,
    deleted_by integer
);

create table parte_trabajo (
    id integer primary key,
    orden_trabajo_id integer not null references orden_trabajo,
    fecha_prevista_inicio_ejecucion timestamp not null,
    clasificacion_parte_trabajo_estado integer not null default 10,
    estado boolean not null default true,
    updated_at timestamp,
    updated_by integer,
    deleted_at timestamp,
    deleted_by integer
);

create table parte_trabajo_tarea (
    id integer primary key,
    parte_trabajo_id integer not null references parte_trabajo
        on delete cascade,
    orden_trabajo_tarea_id integer not null references orden_trabajo_tarea,
    tarea_estado integer not null default 10,
    estado boolean not null default true,
    updated_at timestamp,
    updated_by integer,
    deleted_at timestamp,
    deleted_by integer
);

create table parte_trabajo_tarea_log (
    id serial primary key,
    parte_trabajo_tarea_id integer not null,
    estado boolean,
    fecha timestamp not null default now()
);

create function add_parte_trabajo_tarea_log() returns trigger
language plpgsql as $$
begin
    insert into parte_trabajo_tarea_log (parte_trabajo_tarea_id, estado)
    values (new.id, new.estado);
    return new;
end;
$$;

create trigger add_parte_trabajo_tarea_log
after update on parte_trabajo_tarea
for each row execute function add_parte_trabajo_tarea_log();

create function update_tarea_estado_parte() returns trigger
language plpgsql as $$
begin
    if new.tarea_estado = 90 then
        update orden_trabajo_tarea
        set tarea_estado = 90, estado = false
        where id = new.orden_trabajo_tarea_id;
    end if;
    return new;
end;
$$;

create trigger update_tarea_estado_parte
after update of tarea_estado on parte_trabajo_tarea
for each row execute function update_tarea_estado_parte();

-- Datos ---------------------------------------------------------------------
-- Un parte (1) el 23/11/2026 con dos grupos de tareas duplicadas:
--   * tpm 10: tareas de parte 1 y 2, sin iniciar → se borra la 2.
--   * tpm 11: tarea 4 sin iniciar y tarea 5 iniciada (30) → se conserva la 5
--     y se borra la 4.
-- La tarea 3 (tpm 12) no está duplicada dentro del parte 1, pero la OT 2
-- repite tpm 12 en la misma fecha (tarea 6, parte 2): es una OT duplicada
-- que anular_ot_duplicadas_plan.sql anula.

insert into tarea_plan_mantenimiento values
    (10, 'TPM-10'), (11, 'TPM-11'), (12, 'TPM-12');

insert into orden_trabajo (id, numero_ot) values (1, 'OT-1'), (2, 'OT-2');

insert into tarea_planificacion (id, codigo, tarea_plan_mantenimiento_id,
    fecha_proxima_ejecucion, empresa_ubicacion_id, empresa_activo_id)
values
    (1, 'TP-1', 10, '2026-11-23 08:00', 100, 500),
    (2, 'TP-2', 10, '2026-11-23 08:00', 100, 500),
    (3, 'TP-3', 12, '2026-11-23 08:00', 100, 501),
    (4, 'TP-4', 11, '2026-11-23 08:00', 100, 502),
    (5, 'TP-5', 11, '2026-11-23 08:00', 100, 502),
    (6, 'TP-6', 12, '2026-11-23 08:00', 100, 501);

insert into orden_trabajo_tarea (id, orden_trabajo_id,
    tarea_planificacion_id)
values (1, 1, 1), (2, 1, 2), (3, 1, 3), (4, 1, 4), (5, 1, 5), (6, 2, 6);

insert into parte_trabajo (id, orden_trabajo_id,
    fecha_prevista_inicio_ejecucion)
values (1, 1, '2026-11-23 08:00'), (2, 2, '2026-11-23 08:00');

insert into parte_trabajo_tarea (id, parte_trabajo_id,
    orden_trabajo_tarea_id, tarea_estado)
values (1, 1, 1, 10), (2, 1, 2, 10), (3, 1, 3, 10), (4, 1, 4, 10),
    (5, 1, 5, 30), (6, 2, 6, 10);

analyze;
