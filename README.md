# datasette-extract

[![PyPI](https://img.shields.io/pypi/v/datasette-extract.svg)](https://pypi.org/project/datasette-extract/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-extract?include_prereleases&label=changelog)](https://github.com/datasette/datasette-extract/releases)
[![Tests](https://github.com/datasette/datasette-extract/workflows/Test/badge.svg)](https://github.com/datasette/datasette-extract/actions?query=workflow%3ATest)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-extract/blob/main/LICENSE)

Import unstructured data (text and images) into structured tables

## Installation

Install this plugin in the same environment as [Datasette](https://datasette.io/).
```bash
datasette install datasette-extract
```

## Configuration

This plugin uses the [LLM](https://llm.datasette.io/) library and works with any LLM provider that supports:
- Async models
- JSON schema-based structured output

The plugin automatically discovers available models and their required API keys. Only models with configured API keys will be shown to users.

### Setting up API Keys

You can configure API keys in two ways:

**Option 1: Using environment variables**

Set the appropriate environment variable before starting Datasette:

```bash
# For OpenAI
export DATASETTE_SECRETS_OPENAI_API_KEY="sk-..."

# For Anthropic
export DATASETTE_SECRETS_ANTHROPIC_API_KEY="sk-ant-..."

# For Gemini
export DATASETTE_SECRETS_GEMINI_API_KEY="..."
```

**Option 2: Using the datasette-secrets UI**

The plugin integrates with [datasette-secrets](https://github.com/datasette/datasette-secrets) to let users configure their own API keys through the web interface. Any schema-capable async model will automatically have its required API key registered as a configurable secret.

### Installing Model Providers

First install the LLM plugin for your chosen provider:

**OpenAI** (GPT-4o, GPT-4, etc.):
```bash
llm install llm-openai-plugin
```

**Anthropic Claude**:
```bash
llm install llm-anthropic
```

**Google Gemini**:
```bash
llm install llm-gemini
```

**Other providers**: See the [LLM plugins directory](https://llm.datasette.io/en/stable/plugins/directory.html) for more options.

### Starting Datasette

Once you've installed at least one LLM plugin and configured its API key, start Datasette:

```bash
DATASETTE_SECRETS_OPENAI_API_KEY="sk-..." datasette data.db --root --create
# Now click or command-click the URL containing .../-/auth-token?token=...
```
- The `--root` flag causes Datasette to output a link that will sign you in as root
- The `--create` flag will create the `data.db` SQLite database file if it does not exist

### Restricting Available Models

By default, all schema-capable async models with configured API keys will be available. You can restrict this to specific models using the `models` setting:

```yaml
plugins:
  datasette-extract:
    models:
      - gpt-4o-mini
      - claude-3-5-sonnet-latest
      - gemini-2.0-flash-exp
```
If you only list a single model, users will not see a model selector in the UI.

## Usage

This plugin provides the following features:

- In the database action cog menu for a database select "Create table with extracted data" to create a new table with data extracted from text or an image
- In the table action cog menu select "Extract data into this table" to extract data into an existing table

When creating a table you can specify the column names, types and provide an optional hint (like "YYYY-MM-DD" for dates) to influence how the data should be extracted.

When populating an existing table you can provide hints and select which columns should be populated.

Text input can be pasted directly into the textarea.

Drag and drop a PDF or text file onto the textarea to populate it with the contents of that file. PDF files will have their text extracted, but only if the file contains text as opposed to scanned images.

Drag and drop a single image onto the textarea - or select it with the image file input box - to process an image.

## Permissions

Users must have the `datasette-extract` permission to use this tool.

In order to create tables they also need the `create-table` permission.

To insert rows into an existing table they need `insert-row`.

## Development

The recommended way to develop this plugin uses [uv](https://github.com/astral-sh/uv). To run the tests:
```bash
cd datasette-extract
uv run pytest
```
To run a development server with an OpenAI API key (pulled from the LLM key store):
```bash
DATASETTE_SECRETS_OPENAI_API_KEY="$(llm keys get openai)" \
  uv run datasette data.db --create --root --secret 1 \
  -s plugins.datasette-extract.models '["gpt-4o-mini"]' \
  --internal internal.db --reload
```