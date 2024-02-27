import asyncio
from datasette import hookimpl, Response, NotFound
from openai import AsyncOpenAI, OpenAIError
from sqlite_utils import Database
import click
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


async def extract_create_table(datasette, request):
    database = request.url_vars["database"]
    try:
        db = datasette.get_database(database)
    except KeyError:
        raise NotFound("Database '{}' does not exist".format(database))

    if request.method == "POST":
        post_vars = await request.post_vars()
        content = (post_vars.get("content") or "").strip()
        if not content:
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
            datasette, request, content, database, table, properties
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


async def extract_to_table(datasette, request):
    database = request.url_vars["database"]
    table = request.url_vars["table"]
    # Do they exist?
    try:
        db = datasette.get_database(database)
    except KeyError:
        raise NotFound("Database '{}' does not exist".format(database))
    tables = await db.table_names()
    if table not in tables:
        raise NotFound("Table '{}' does not exist".format(table))

    schema = await db.execute_fn(lambda conn: Database(conn)[table].columns_dict)

    if request.method == "POST":
        # Turn schema into a properties dict
        properties = {
            name: {
                "type": get_type(type_),
                # "description": "..."
            }
            for name, type_ in schema.items()
        }
        post_vars = await request.post_vars()
        content = (post_vars.get("content") or "").strip()
        return await extract_to_table_post(
            datasette, request, content, database, table, properties
        )

    return Response.html(
        await datasette.render_template(
            "extract_to_table.html",
            {
                "database": database,
                "table": table,
                "schema": schema,
            },
            request=request,
        )
    )


async def extract_table_task(datasette, database, table, properties, content, task_id):
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

    async_client = AsyncOpenAI()
    db = datasette.get_database(database)

    def make_row_writer(row):
        def _write(conn):
            with conn:
                db = Database(conn)
                db[table].insert(row)

        return _write

    try:
        async for chunk in await async_client.chat.completions.create(
            stream=True,
            model="gpt-4-turbo-preview",
            messages=[{"role": "user", "content": content}],
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

    except OpenAIError as ex:
        task_info["error"] = str(ex)
        return
    finally:
        task_info["done"] = True


async def extract_to_table_post(
    datasette, request, content, database, table, properties
):
    # Here we go!
    if not content:
        return Response.text("No content provided")

    task_id = str(ulid.ULID())
    asyncio.create_task(
        extract_table_task(datasette, database, table, properties, content, task_id)
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


@click.command()
@click.argument(
    "database",
    type=click.Path(file_okay=True, dir_okay=False, allow_dash=False),
    required=True,
)
@click.argument("table", required=True)
def extract(database, table):
    click.echo("Will extract to {} in {}".format(table, database))


@hookimpl
def register_commands(cli):
    cli.add_command(extract, name="extract")


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
