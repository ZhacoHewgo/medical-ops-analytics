def execute_schema(cursor, path):
    for statement in path.read_text(encoding="utf-8").split(";"):
        if statement.strip():
            cursor.execute(statement)
