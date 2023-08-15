from datasette import hookimpl, Response
import click


async def extract_web(datasette, request):
    return Response.html(
        await datasette.render_template("extract.html", request=request)
    )


@click.command()
def extract():
    click.echo("Hello from extract")


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
        (r"^/-/extract$", extract_web),
    ]
