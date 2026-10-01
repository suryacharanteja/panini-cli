import codecs
import hashlib
import json

import pytest
from panini_cli.agent import tools
from panini_cli.agent.tools import ToolError, Workspace


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "node_modules" / "lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "lib" / "index.js").write_text("def add", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[remote] url = https://token@example.com", encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-secret", encoding="utf-8")
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=", encoding="utf-8")
    return Workspace(tmp_path)


def test_paths_never_leave_the_workspace(ws, tmp_path):
    for escape in ("../outside.txt", str(tmp_path.parent / "outside.txt"), "src/../../outside.txt"):
        with pytest.raises(ToolError, match="outside the workspace"):
            ws.resolve(escape, must_exist=False)
    with pytest.raises(ToolError, match="managed by git"):
        tools.read_file(ws, ".git/config")
    assert ws.label(ws.resolve("src/app.py")) == "src/app.py"


def test_secret_files_are_never_read_written_or_searched(ws):
    with pytest.raises(ToolError, match="secrets"):
        tools.read_file(ws, ".env")
    with pytest.raises(ToolError, match="secrets"):
        tools.prepare_write(ws, ".env", "X=1")
    with pytest.raises(ToolError, match="secrets"):
        tools.prepare_write(ws, "certs/server.pem", "-----BEGIN")
    assert "OPENAI_API_KEY" in tools.read_file(ws, ".env.example")
    result = tools.search_files(ws, "OPENAI_API_KEY")
    assert ".env.example" in result and "sk-secret" not in result


def test_read_file_numbers_lines_and_pages_long_files(ws):
    assert tools.read_file(ws, "src/app.py").splitlines()[1:] == [
        "     1\tdef add(a, b):",
        "     2\t    return a + b",
    ]
    ws.resolve("big.txt", must_exist=False).write_text("\n".join(f"line {n}" for n in range(1, 1501)))
    first = tools.read_file(ws, "big.txt")
    assert "lines 1-1000 of 1500" in first and "continue with start_line=1001" in first
    assert "lines 1490-1500 of 1500" in tools.read_file(ws, "big.txt", 1490)
    ws.resolve("blob.bin", must_exist=False).write_bytes(b"\0\1\2" * 10)
    assert "binary file" in tools.read_file(ws, "blob.bin")


def test_list_and_search_skip_dependencies_and_git(ws):
    listing = tools.list_files(ws)
    assert "src/" in listing and "  app.py" in listing
    assert "node_modules/ (skipped" in listing and ".git" not in listing
    matches = tools.search_files(ws, r"def add")
    assert "src/app.py:1: def add(a, b):" in matches and "node_modules" not in matches
    assert tools.search_files(ws, "DEF ADD", case_sensitive=True).startswith("No matches")
    assert "src/app.py" in tools.search_files(ws, "return", glob="*.py")
    with pytest.raises(ToolError, match="Invalid regular expression"):
        tools.search_files(ws, "(unclosed")


def test_search_code_bounded_hashed_and_secret_safe(ws):
    result = json.loads(tools.search_code(ws, "add", max_results=1))
    assert result["incomplete"] is False
    assert len(result["results"]) == 1
    hit = result["results"][0]
    assert (hit["path"], hit["start_line"], hit["end_line"]) == ("src/app.py", 1, 1)
    assert hit["line_count"] == 1
    assert hit["full_excerpt_included"] is False
    assert hit["source_sha256"] == hashlib.sha256(ws.resolve("src/app.py").read_bytes()).hexdigest()
    assert result["selection"]["role"] == "evaluator_rerank_only"
    assert "read_file" in result["next_step"]
    assert "node_modules" not in json.dumps(result)
    assert "sk-secret" not in json.dumps(json.loads(tools.search_code(ws, "OPENAI_API_KEY")))
    with pytest.raises(ToolError, match="secrets"):
        tools.search_code(ws, "secret", path=".env")
    with pytest.raises(ToolError, match="outside the workspace"):
        tools.search_code(ws, "add", path="../outside")


def test_search_code_marks_truncation(ws):
    ws.resolve("many.py", must_exist=False).write_text("\n".join("add" for _ in range(30)))
    result = json.loads(tools.search_code(ws, "add", max_results=2))
    assert len(result["results"]) == 2
    assert result["incomplete"] is True
    assert result["candidate_count"] > 2


def test_search_code_jev_reranks_without_dropping_matches(ws):
    ws.resolve("other.py", must_exist=False).write_text("add helper\n")
    observed = []

    def selector(query, candidates):
        observed.extend(candidates)
        return list(reversed(range(len(candidates)))), {"mode": "jev", "cost_usd": 0.001, "input_tokens": 12}

    result = json.loads(tools.search_code(ws, "add", selector=selector))
    assert len(observed) == len(result["results"]) == 2
    # The selector reversed the lexical order: its last candidate is now first.
    assert result["results"][0]["path"] == observed[-1][1]
    assert result["selection"]["mode"] == "jev"
    assert result["selection"]["role"] == "evaluator_rerank_only"
    assert result["selection"]["input_tokens"] == 12

    def failed_selector(query, candidates):
        raise RuntimeError("unavailable")

    fallback = json.loads(tools.search_code(ws, "add", selector=failed_selector))
    assert len(fallback["results"]) == 2
    assert fallback["selection"]["mode"] == "lexical_fallback"


def test_edit_requires_a_unique_exact_match_and_preserves_line_endings_and_bom(ws):
    path = ws.resolve("win.txt", must_exist=False)
    path.write_bytes(codecs.BOM_UTF8 + b"alpha\r\nbeta\r\nbeta\r\n")
    with pytest.raises(ToolError, match="not found"):
        tools.prepare_edit(ws, "win.txt", "gamma", "delta")
    with pytest.raises(ToolError, match="appears 2 times"):
        tools.prepare_edit(ws, "win.txt", "beta", "BETA")
    change = tools.prepare_edit(ws, "win.txt", "alpha\nbeta", "ALPHA\nbeta")
    assert change.kind == "update" and "-alpha" in change.diff and "+ALPHA" in change.diff
    tools.apply_change(change)
    assert path.read_bytes() == codecs.BOM_UTF8 + b"ALPHA\r\nbeta\r\nbeta\r\n"
    tools.apply_change(tools.prepare_edit(ws, "win.txt", "beta", "BETA", replace_all=True))
    assert path.read_bytes() == codecs.BOM_UTF8 + b"ALPHA\r\nBETA\r\nBETA\r\n"


def test_write_creates_files_and_refuses_stale_proposals(ws):
    change = tools.prepare_write(ws, "pkg/new.py", "print('hi')\n")
    assert change.kind == "add" and change.diff.startswith("--- /dev/null")
    tools.apply_change(change)
    assert ws.resolve("pkg/new.py").read_text() == "print('hi')\n"
    with pytest.raises(ToolError, match="already has exactly this content"):
        tools.prepare_write(ws, "pkg/new.py", "print('hi')\n")
    stale = tools.prepare_edit(ws, "src/app.py", "a + b", "b + a")
    ws.resolve("src/app.py").write_text("changed by the user\n")
    with pytest.raises(ToolError, match="changed after this edit was proposed"):
        tools.apply_change(stale)
    assert ws.resolve("src/app.py").read_text() == "changed by the user\n"


def test_tool_schemas_are_strict():
    for tool in tools.TOOLS:
        parameters = tool["parameters"]
        assert tool["strict"] and parameters["additionalProperties"] is False
        assert sorted(parameters["required"]) == sorted(parameters["properties"])
