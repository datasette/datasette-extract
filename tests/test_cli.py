from datasette.cli import cli
from click.testing import CliRunner


def test_extract_command():
    runner = CliRunner()
    result = runner.invoke(cli, ["extract"])
    assert result.exit_code == 0
    assert result.output == "Hello from extract\n"
