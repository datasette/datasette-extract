import asyncio
import llm
from datasette import hookimpl, Response, NotFound, Permission, Forbidden
from datasette_secrets import Secret, get_secret
from datetime import datetime, timezone
from sqlite_utils import Database
from starlette.requests import Request as StarletteRequest
import ijson
import json
import ulid
import urllib


@hookimpl
def register_permissions(datasette):
    return [
        Permission(
            name="datasette-extract",
            abbr=None,
            description="Use the extract tool to populate tables",
            takes_database=False,
            takes_resource=False,
            default=False,
        )
    ]


@hookimpl
def register_secrets():
    return [
        Secret(
            name="OPENAI_API_KEY",
            obtain_label="Get an OpenAI API key",
            obtain_url="https://platform.openai.com/api-keys",
        ),
    ]


@hookimpl
def permission_allowed(action, actor):
    if action == "datasette-extract" and actor and actor.get("id") == "root":
        return True


def get_config(datasette):
    return datasette.plugin_config("datasette-extract") or {}


async def can_extract(datasette, actor, database_name, to_table=None):
    if actor is None:
        return False
    reply_from_that = await datasette.permission_allowed(actor, "datasette-extract")
    if not reply_from_that:
        return False
    if not to_table:
        # Need create-table for database
        can_create_table = await datasette.permission_allowed(
            actor, "create-table", resource=database_name
        )
        if not can_create_table:
            return False
        return True
    else:
        # Need insert-row for that table
        return await datasette.permission_allowed(
            actor, "insert-row", resource=(database_name, to_table)
        )


def image_is_provided(image):
    # UploadFile(filename='', size=0, headers=Headers...
    return bool(image.size)


async def extract_create_table(datasette, request, scope, receive):
    database = request.url_vars["database"]
    try:
        datasette.get_database(database)
    except KeyError:
        raise NotFound("Database '{}' does not exist".format(database))

    if not await can_extract(datasette, request.actor, database):
        raise Forbidden("Permission denied to extract data")

    if request.method == "POST":
        starlette_request = StarletteRequest(scope, receive)
        post_vars = await starlette_request.form()
        content = (post_vars.get("content") or "").strip()
        image = post_vars.get("image") or ""
        instructions = post_vars.get("instructions") or ""
        if not content and not image_is_provided(image) and not instructions:
            return Response.text("No content provided", status=400)
        table = post_vars.get("table")
        if not table:
            return Response.text("No table provided", status=400)

        model_id = post_vars["model"]

        properties = {}
        # Build the properties out of name_0 upwards, only if populated
        for key, value in post_vars.items():
            if key.startswith("name_") and value.strip():
                index = int(key.split("_")[1])
                type_ = post_vars.get("type_{}".format(index))
                hint = post_vars.get("hint_{}".format(index))
                properties[value] = {
                    "type": type_,
                }
                if hint:
                    properties[value]["description"] = hint

        return await extract_to_table_post(
            datasette,
            request,
            model_id,
            instructions,
            content,
            image,
            database,
            table,
            properties,
        )

    fields = []
    if "_fields" in request.args:
        try:
            fields = [
                field
                for field in json.loads(request.args["_fields"])
                if isinstance(field, dict) and isinstance(field.get("index"), int)
            ]
        except (json.JSONDecodeError, TypeError):
            fields = []
    if not fields:
        fields = [{"index": i} for i in range(10)]

    models = [
        {"id": model.model_id, "name": str(model)}
        for model in llm.get_async_models()
        if model.supports_schema
    ]

    config = get_config(datasette)
    if config.get("models"):
        models = [model for model in models if model["id"] in config["models"]]

    return Response.html(
        await datasette.render_template(
            "extract_create_table.html",
            {
                "database": database,
                "fields": fields,
                "models": models,
            },
            request=request,
        )
    )


async def extract_to_table(datasette, request, scope, receive):
    database = request.url_vars["database"]
    table = request.url_vars["table"]
    # Do they exist?
    try:
        db = datasette.get_database(database)
    except KeyError:
        raise NotFound("Database '{}' does not exist".format(database))

    if not await can_extract(datasette, request.actor, database, table):
        raise Forbidden("Permission denied to extract data")

    tables = await db.table_names()
    if table not in tables:
        raise NotFound("Table '{}' does not exist".format(table))

    schema = await db.execute_fn(lambda conn: Database(conn)[table].columns_dict)

    if request.method == "POST":
        starlette_request = StarletteRequest(scope, receive)
        post_vars = await starlette_request.form()

        # We only use columns that have their use_{colname} set
        use_columns = [
            key[len("use_") :]
            for key, value in post_vars.items()
            if key.startswith("use_") and value
        ]

        # Grab all of the hints
        column_hints = {
            key[len("hint_") :]: value.strip()
            for key, value in post_vars.items()
            if key.startswith("hint_") and value.strip()
        }
        # Turn schema into a properties dict
        properties = {}
        for name, type_ in schema.items():
            if name in use_columns:
                properties[name] = {"type": get_type(type_)}
                description = column_hints.get(name) or ""
                if description:
                    properties[name]["description"] = description

        image = post_vars.get("image") or ""
        instructions = post_vars.get("instructions") or ""
        content = (post_vars.get("content") or "").strip()
        model_id = post_vars["model"]
        return await extract_to_table_post(
            datasette,
            request,
            model_id,
            instructions,
            content,
            image,
            database,
            table,
            properties,
        )

    # GET request logic starts here
    # Restore properties from previous run, if possible
    previous_runs = []
    if await db.table_exists("_datasette_extract"):
        previous_runs = [
            dict(row)
            for row in (
                await db.execute(
                    """
            select id, database_name, table_name, created, properties, instructions, completed, error, num_items
            from _datasette_extract
            where database_name = :database_name and table_name = :table_name
            order by id desc limit 20
        """,
                    {"database_name": database, "table_name": table},
                )
            ).rows
        ]

    columns = [
        {"name": name, "type": value, "hint": "", "checked": True}
        for name, value in schema.items()
    ]

    instructions = ""

    # If there are previous runs, use the properties from the last one to update columns
    if previous_runs:
        properties = json.loads(previous_runs[0]["properties"])
        for column in columns:
            column_name = column["name"]
            column["checked"] = column_name in properties
            column["hint"] = (properties.get(column_name) or {}).get(
                "description"
            ) or ""
        instructions = previous_runs[0]["instructions"] or ""

    duplicate_url = (
        datasette.urls.database(database)
        + "/-/extract?"
        + urllib.parse.urlencode(
            {
                "_fields": json.dumps(
                    [
                        {
                            "index": i,
                            "name": col["name"],
                            "type": col["type"].__name__,
                            "hint": col["hint"],
                        }
                        for i, col in enumerate(columns)
                    ]
                )
            }
        )
    )

    # Fetch models for the template (copied from extract_create_table)
    models = [
        {"id": model.model_id, "name": str(model)}
        for model in llm.get_async_models()
        if model.supports_schema
    ]
    config = get_config(datasette)
    if config.get("models"):
        models = [model for model in models if model["id"] in config["models"]]

    return Response.html(
        await datasette.render_template(
            "extract_to_table.html",
            {
                "database": database,
                "table": table,
                "schema": schema,
                "columns": columns,
                "instructions": instructions,
                "duplicate_url": duplicate_url,
                "previous_runs": previous_runs,
                "models": models,
            },
            request=request,
        )
    )


async def extract_table_task(
    datasette,
    model_id,
    database,
    table,
    properties,
    instructions,
    content,
    image,
    task_id,
):
    # This task runs in the background and writes to the table as it extracts rows
    events = ijson.sendable_list()
    coro = ijson.items_coro(events, "items.item", use_float=True)
    seen_events = set()
    items = []

    datasette._extract_tasks = getattr(datasette, "_extract_tasks", None) or {}
    task_info = {
        "items": items,
        "database": database,
        "model": model_id,
        "table": table,
        "instructions": instructions,
        "properties": properties,
        "error": None,
        "done": False,
    }
    datasette._extract_tasks[task_id] = task_info

    # We record tasks to the _datasette_extract table, mainly so we can reuse
    # property definitions later on
    def start_write(conn):
        with conn:
            db = Database(conn)
            db["_datasette_extract"].insert(
                {
                    "id": task_id,
                    "database_name": database,
                    "table_name": table,
                    "created": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "model": model_id,
                    "instructions": instructions.strip() or None,
                    "properties": json.dumps(properties),
                    "completed": None,
                    "error": None,
                    "num_items": 0,
                },
                pk="id",
                alter=True,
                column_order=(  # Define order explicitly
                    "id",
                    "database_name",
                    "table_name",
                    "created",
                    "model",
                    "instructions",
                    "properties",
                    "completed",
                    "error",
                    "num_items",
                ),
            )

    db = datasette.get_database(database)

    # Ensure table exists before writing
    await db.execute_write_fn(
        lambda conn: Database(conn)["_datasette_extract"].create(
            {
                "id": str,
                "database_name": str,
                "table_name": str,
                "created": str,
                "model": str,
                "instructions": str,
                "properties": str,
                "completed": str,
                "error": str,
                "num_items": int,
            },
            pk="id",
            if_not_exists=True,
        )
    )

    await db.execute_write_fn(start_write)

    def make_row_writer(row):
        def _write(conn):
            with conn:
                db = Database(conn)
                db[table].insert(row)

        return _write

    error = None

    try:
        model = llm.get_async_model(model_id)
        kwargs = {}
        if instructions:
            kwargs["system"] = instructions
        if image_is_provided(image):
            image_bytes = await image.read()
            kwargs["attachments"] = [llm.Attachment(content=image_bytes)]
        if content:
            kwargs["prompt"] = content

        kwargs["schema"] = {
            "type": "object",
            "description": "Extract data",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": properties,
                        "required": list(properties.keys()),
                    },
                }
            },
            "required": ["items"],
        }

        async for chunk in model.prompt(**kwargs):
            if chunk:
                coro.send(chunk.encode("utf-8"))
                if events:
                    # Any we have not seen yet?
                    unseen_events = [
                        e for e in events if json.dumps(e) not in seen_events
                    ]
                    if unseen_events:
                        for event in unseen_events:
                            event = remove_null_bytes(event)
                            seen_events.add(json.dumps(event))
                            items.append(event)
                            await db.execute_write_fn(make_row_writer(event))

    except Exception as ex:
        task_info["error"] = str(ex)
        error = str(ex)
    finally:
        task_info["done"] = True

        def end_write(conn):
            with conn:
                db = Database(conn)
                db["_datasette_extract"].update(
                    task_id,
                    {
                        "completed": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "num_items": len(items),
                        "error": error,
                    },
                )

        await db.execute_write_fn(end_write)


async def extract_to_table_post(
    datasette,
    request,
    model_id,
    instructions,
    content,
    image,
    database,
    table,
    properties,
):
    # Here we go!
    if not content and not image_is_provided(image) and not instructions:
        return Response.text("No content provided", status=400)

    task_id = str(ulid.ULID())

    asyncio.create_task(
        extract_table_task(
            datasette,
            model_id,
            database,
            table,
            properties,
            instructions,
            content,
            image,
            task_id,
        )
    )
    return Response.redirect(
        datasette.urls.path("/-/extract/progress/{}".format(task_id))
    )


def get_task_info(datasette, task_id):
    extract_tasks = getattr(datasette, "_extract_tasks", None) or {}
    return extract_tasks.get(task_id)


async def extract_progress(datasette, request):
    task_info = get_task_info(datasette, request.url_vars["task_id"])
    if not task_info:
        return Response.text("Task not found", status=404)
    return Response.html(
        await datasette.render_template(
            "extract_progress.html",
            {
                "task": task_info,
                "table_url": datasette.urls.table(
                    task_info["database"], task_info["table"]
                ),
            },
            request=request,
        )
    )


async def extract_progress_json(datasette, request):
    task_info = get_task_info(datasette, request.url_vars["task_id"])
    if not task_info:
        return Response.json({"ok": False, "error": "Task not found"}, status=404)
    return Response.json(task_info)


@hookimpl
def register_routes():
    return [
        (r"^/(?P<database>[^/]+)/-/extract$", extract_create_table),
        (r"^/(?P<database>[^/]+)/(?P<table>[^/]+)/-/extract$", extract_to_table),
        (r"^/-/extract/progress/(?P<task_id>\w+)$", extract_progress),
        (r"^/-/extract/progress/(?P<task_id>\w+)\.json$", extract_progress_json),
    ]


def get_type(type_):
    if type_ is int:
        return "integer"
    elif type_ is float:
        return "number"
    else:
        return "string"


@hookimpl
def database_actions(datasette, actor, database):
    async def inner():
        if not await get_secret(datasette, "OPENAI_API_KEY"):
            return
        if not await can_extract(datasette, actor, database):
            return
        return [
            {
                "href": datasette.urls.database(database) + "/-/extract",
                "label": "Create table with AI extracted data",
                "description": "Paste in text or an image to extract structured data",
            }
        ]

    return inner


@hookimpl
def table_actions(datasette, actor, database, table):
    async def inner():
        if not await get_secret(datasette, "OPENAI_API_KEY"):
            return
        if not await can_extract(datasette, actor, database, table):
            return
        return [
            {
                "href": datasette.urls.table(database, table) + "/-/extract",
                "label": "Extract data into this table with AI",
                "description": "Paste in text or an image to extract structured data",
            }
        ]

    return inner


def remove_null_bytes(data: dict) -> dict:
    """
    Recursively removes null bytes (u0000) from string values in a dictionary with JSON semantics.
    """
    if isinstance(data, dict):
        return {key: remove_null_bytes(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [remove_null_bytes(item) for item in data]
    elif isinstance(data, str):
        return data.replace("\u0000", "")
    else:
        return data
