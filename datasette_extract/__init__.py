import click
from datasette import hookimpl, Response, NotFound
import json
import openai
from sqlite_utils import Database
import sys


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


async def extract_to_table_post(
    datasette, request, content, database, table, properties
):
    # Here we go!
    if not content:
        return Response.text("No content provided")

    required_fields = list(properties.keys())
    try:
        contents = []
        async for chunk in await openai.ChatCompletion.acreate(
            stream=True,
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": content}],
            functions=[
                {
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
                                    "required": required_fields,
                                },
                            }
                        },
                        "required": ["items"],
                    },
                },
            ],
            function_call={"name": "extract_data"},
        ):
            content = (
                chunk["choices"][0]
                .get("delta", {})
                .get("function_call", {})
                .get("arguments")
            )
            print(content, end="")
            sys.stdout.flush()
            if content is not None:
                contents.append(content)

    except openai.OpenAIError as ex:
        return Response.text(str(ex), status=400)
    output = "".join(contents)
    return Response.json(json.loads(output))


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
def startup():
    # TODO: Create tables etc, maybe using sqlite-migrate
    pass


@hookimpl
def register_routes():
    return [
        (r"^/-/extract/(?P<database>[^/]+)$", extract_create_table),
        (r"^/-/extract/(?P<database>[^/]+)/(?P<table>[^/]+)$", extract_to_table),
    ]


def get_type(type_):
    if type_ is int:
        return "integer"
    elif type_ is float:
        return "number"
    else:
        return "string"
