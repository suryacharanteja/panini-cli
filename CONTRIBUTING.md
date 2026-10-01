# Contributing

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m ruff check src tests evals
.venv\Scripts\python -m pytest -q
```

The test suite runs offline: models are scripted (`ScriptedModel`), Ollama and web calls use mocked HTTP transports,
and MCP uses `tests/fake_mcp_server.py`. Native-sandbox tests skip where Windows 11's sandbox API is unavailable.

## Changing routing, prompts or tools

These change what the agent solves, so they need evidence, not only passing tests:

1. Run the paired eval before and after (real models, costs a little):

   ```powershell
   .venv\Scripts\python evals/run.py --arm auto --arm model:gpt-5.6-terra
   ```

2. Keep the change only if it does not lose solves, and cuts cost or time. Commit the result file from `evals/results/`
   with the change.
3. Add tasks to `evals/tasks.json` when you fix a failure, so it stays fixed.

## Style

Match the surrounding code: small modules, plain functions, docstrings that say *why*, and errors the model or the user
can act on. Keep the safety properties in [SECURITY.md](SECURITY.md) intact.
