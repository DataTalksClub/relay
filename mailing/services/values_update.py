"""Fast multi-row UPDATE for PostgreSQL: ``UPDATE t SET ... FROM (VALUES ...) v WHERE t.pk = v.pk``.

Django's ``bulk_update`` builds one ``CASE WHEN pk=... THEN ...`` expression per
row per field. Resolving those expressions costs ~2.4 s of Python CPU per 500
row-updates (cProfile, 2026-11-24), and callers run it while holding row locks
(provider-events batches lock up to 100 recipient rows; the campaign sender holds
its claimed rows). On a 1-CPU worker that left recipient rows locked for seconds
after ``INSERT INTO email_events`` and blocked the sender's claims.

This builds the statement directly, with one typed placeholder per value, so its
cost is linear and tiny. Other database vendors (SQLite in unit tests) fall back
to ``bulk_update`` with the same filters and return value.
"""

from django.db import connections, router

_TEXT_TYPES = {"CharField", "TextField", "SlugField", "URLField", "EmailField"}


def _cast_type(field, connection):
    # CAST(... AS varchar(n)) silently truncates; cast text to text so an over-long
    # value fails on assignment exactly like a normal UPDATE would.
    if field.get_internal_type() in _TEXT_TYPES:
        return "text"
    return field.db_type(connection)


def values_update(model, objs, fields, *, where=None, using=None):
    """Update ``fields`` of ``objs`` (matched by pk) in one statement; returns rows matched.

    ``where`` is an optional ``{field_name: value | list | tuple | set}`` filter
    applied to the target rows (a collection means ``IN``). Every obj must have a
    distinct pk.
    """
    objs = list(objs)
    if not objs:
        return 0
    using = using or router.db_for_write(model)
    connection = connections[using]
    where = dict(where or {})
    if connection.vendor != "postgresql":
        lookups = {}
        for name, value in where.items():
            if isinstance(value, (list, tuple, set, frozenset)):
                lookups[f"{name}__in"] = list(value)
            else:
                lookups[name] = value
        return model._base_manager.using(using).filter(**lookups).bulk_update(objs, fields)

    opts = model._meta
    pk_field = opts.pk
    if len({obj.pk for obj in objs}) != len(objs):
        raise ValueError("values_update: duplicate primary keys")
    model_fields = [opts.get_field(name) for name in fields]
    columns = [pk_field, *model_fields]
    qn = connection.ops.quote_name

    row_sql = "(" + ", ".join(f"CAST(%s AS {_cast_type(field, connection)})" for field in columns) + ")"
    params = []
    for obj in objs:
        params.append(pk_field.get_db_prep_value(obj.pk, connection, prepared=False))
        for field in model_fields:
            params.append(field.get_db_prep_save(getattr(obj, field.attname), connection))

    alias_columns = ", ".join(qn(field.column) for field in columns)
    assignments = ", ".join(f"{qn(field.column)} = v.{qn(field.column)}" for field in model_fields)
    conditions = [f"t.{qn(pk_field.column)} = v.{qn(pk_field.column)}"]
    for name, value in where.items():
        field = opts.get_field(name)
        column = f"t.{qn(field.column)}"
        if isinstance(value, (list, tuple, set, frozenset)):
            values = list(value)
            if not values:
                return 0
            conditions.append(f"{column} IN ({', '.join(['%s'] * len(values))})")
            params.extend(field.get_db_prep_value(item, connection, prepared=False) for item in values)
        else:
            conditions.append(f"{column} = %s")
            params.append(field.get_db_prep_value(value, connection, prepared=False))

    sql = (
        f"UPDATE {qn(opts.db_table)} AS t SET {assignments} "
        f"FROM (VALUES {', '.join([row_sql] * len(objs))}) AS v ({alias_columns}) "
        f"WHERE {' AND '.join(conditions)}"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount
