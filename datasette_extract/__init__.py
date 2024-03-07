import asyncio
import base64
from datasette import hookimpl, Response, NotFound, Permission, Forbidden
from datetime import datetime, timezone
from openai import AsyncOpenAI, OpenAIError
from sqlite_utils import Database
from starlette.requests import Request as StarletteRequest
import ijson
import json
import ulid


CREATE_JOB_TABLE_SQL = """
create table if not exists _extract_jobs (
    id integer primary key,
    database_name text,
    table_name text,
    content text,
    custom_prompt text, -- for custom system prompt
    properties text, -- JSON function properties definition
    started_at text, -- ISO8601 when added
    finished_at text, -- ISO8601 when completed or cancelled
    row_count integer, -- starts at 0
    error text
);
"""


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
def permission_allowed(action, actor):
    if action == "datasette-extract" and actor and actor.get("id") == "root":
        return True


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


async def extract_create_table(datasette, request, scope, receive):
    database = request.url_vars["database"]
    try:
        db = datasette.get_database(database)
    except KeyError:
        raise NotFound("Database '{}' does not exist".format(database))

    if not await can_extract(datasette, request.actor, database):
        raise Forbidden("Permission denied to extract data")

    if request.method == "POST":
        starlette_request = StarletteRequest(scope, receive)
        post_vars = await starlette_request.form()
        content = (post_vars.get("content") or "").strip()
        image = post_vars.get("image") or ""
        if not content and not image:
            return Response.text("No content provided", status=400)
        table = post_vars.get("table")
        if not table:
            return Response.text("No table provided", status=400)

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
            datasette, request, content, image, database, table, properties
        )

    return Response.html(
        await datasette.render_template(
            "extract_create_table.html",
            {
                "database": database,
                "fields": range(10),
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
        content = (post_vars.get("content") or "").strip()
        return await extract_to_table_post(
            datasette, request, content, image, database, table, properties
        )

    # Restore properties from previous run, if possible
    previous_runs = []
    if await db.table_exists("_datasette_extract"):
        previous_runs = [
            dict(row)
            for row in (
                await db.execute(
                    """
            select id, database_name, table_name, created, properties, completed, error, num_items
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

    # If there are previous runs, use the properties from the last one to update columns
    if previous_runs:
        properties = json.loads(previous_runs[0]["properties"])
        for column in columns:
            column_name = column["name"]
            column["checked"] = column_name in properties
            column["hint"] = (properties.get(column_name) or {}).get(
                "description"
            ) or ""

    return Response.html(
        await datasette.render_template(
            "extract_to_table.html",
            {
                "database": database,
                "table": table,
                "schema": schema,
                "columns": columns,
                "previous_runs": previous_runs,
            },
            request=request,
        )
    )


async def extract_table_task(
    datasette, database, table, properties, content, image, task_id
):
    # This task runs in the background
    events = ijson.sendable_list()
    coro = ijson.items_coro(events, "items.item")
    seen_events = set()
    items = []

    datasette._extract_tasks = getattr(datasette, "_extract_tasks", None) or {}
    task_info = {
        "items": items,
        "database": database,
        "table": table,
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
                    "properties": json.dumps(properties),
                    "completed": None,
                    "error": None,
                    "num_items": 0,
                },
                pk="id",
            )

    async_client = AsyncOpenAI()
    db = datasette.get_database(database)

    await db.execute_write_fn(start_write)

    def make_row_writer(row):
        def _write(conn):
            with conn:
                db = Database(conn)
                db[table].insert(row)

        return _write

    error = None

    async def ocr_image(image_bytes):
        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        messages = [
            {
                "role": "system",
                "content": "Run OCR and return all of the text in this image, with newlines where appropriate",
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                    }
                ],
            },
        ]
        response = await async_client.chat.completions.create(
            model="gpt-4-vision-preview", messages=messages, max_tokens=400
        )
        return response.choices[0].message.content

    try:
        messages = []
        if content:
            messages.append({"role": "user", "content": content})
        if image:
            # Run a separate thing to OCR the image first, because gpt-4-vision can't handle tools yet
            image_content = await ocr_image(await image.read())
            if image_content:
                messages.append({"role": "user", "content": image_content})
            else:
                raise ValueError("Could not extract text from image")

        async for chunk in await async_client.chat.completions.create(
            stream=True,
            model="gpt-4-turbo-preview",
            messages=messages,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "extract_data",
                        "description": "Extract data matching this schema",
                        "parameters": {
                            "type": "object",
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
                        },
                    },
                },
            ],
            tool_choice={"type": "function", "function": {"name": "extract_data"}},
            max_tokens=4096,
        ):
            try:
                content = chunk.choices[0].delta.tool_calls[0].function.arguments
            except (AttributeError, TypeError):
                content = None
            if content:
                coro.send(content.encode("utf-8"))
                if events:
                    # Any we have not seen yet?
                    unseen_events = [
                        e for e in events if json.dumps(e) not in seen_events
                    ]
                    if unseen_events:
                        for event in unseen_events:
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
    datasette, request, content, image, database, table, properties
):
    # Here we go!
    if not content and not image:
        return Response.text("No content provided")

    task_id = str(ulid.ULID())

    asyncio.create_task(
        extract_table_task(
            datasette, database, table, properties, content, image, task_id
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


async def can_use_extract(datasette, actor, database, table=None):
    # TODO: Add proper permissions checks
    return True


@hookimpl
def database_actions(datasette, actor, database):
    async def inner():
        if not await can_use_extract(datasette, actor, database):
            return []
        return [
            {
                "href": datasette.urls.database(database) + "/-/extract",
                "label": "Create table with extracted data",
            }
        ]

    return inner


@hookimpl
def table_actions(datasette, actor, database, table):
    async def inner():
        if not await can_use_extract(datasette, actor, database, table):
            return []
        return [
            {
                "href": datasette.urls.table(database, table) + "/-/extract",
                "label": "Extract data into this table",
            }
        ]

    return inner
