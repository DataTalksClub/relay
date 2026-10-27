"""Insert a queryset projection without materializing a model per recipient."""
from django.db import connections, models
from django.utils import timezone


def insert_select(model, source, values, *, conflict_fields=()):
    connection = connections[source.db]
    quote = connection.ops.quote_name
    fields = [field for field in model._meta.concrete_fields if not isinstance(field, models.AutoField)]
    expressions = {}
    now = timezone.now()
    for index, field in enumerate(fields):
        value = values.get(field.attname)
        if field.attname not in values:
            value = now if getattr(field, "auto_now_add", False) or getattr(field, "auto_now", False) else field.get_default()
        expressions[f"insert_{index}"] = value if hasattr(value, "resolve_expression") else models.Value(value, output_field=field)
    projection = source.order_by().annotate(**expressions).values(*expressions)
    sql, params = projection.query.get_compiler(using=source.db).as_sql()
    columns = ", ".join(quote(field.column) for field in fields)
    statement = f"INSERT INTO {quote(model._meta.db_table)} ({columns}) {sql}"
    if conflict_fields:
        targets = ", ".join(quote(model._meta.get_field(name).column) for name in conflict_fields)
        statement += f" ON CONFLICT ({targets}) DO NOTHING"
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        return cursor.rowcount
