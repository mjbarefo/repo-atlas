"""Tree-sitter-backed source classification and fact extraction."""

import ast
from collections.abc import Iterator
from pathlib import Path
import re

from tree_sitter import Language, Node, Parser
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript

from .facts import ImportFact, SymbolTable

LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}


def classify(path: Path) -> str | None:
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower())


def _parser(path: Path, language: str) -> Parser:
    if language == "python":
        grammar = tree_sitter_python.language()
    elif path.suffix.lower() == ".tsx":
        grammar = tree_sitter_typescript.language_tsx()
    elif language == "typescript":
        grammar = tree_sitter_typescript.language_typescript()
    else:
        grammar = tree_sitter_javascript.language()
    return Parser(Language(grammar))


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.children:
        yield from _walk(child)


def _text(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _from_import_candidate(base: str, imported: str) -> str:
    if base.endswith("."):
        return f"{base}{imported}"
    return f"{base}.{imported}"


def _python_facts(
    path: Path, source: bytes
) -> tuple[list[str], list[ImportFact], list[str]]:
    # Python's AST supplies precise import semantics after tree-sitter has
    # established the common parsing boundary used for every language.
    module = ast.parse(source, filename=str(path))
    definitions: list[str] = []
    imports: list[ImportFact] = []
    exports: list[str] = []

    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions.append(node.name)
        elif isinstance(node, ast.Import):
            imports.extend(
                ImportFact(
                    alias.name,
                    node.lineno,
                    symbols=(alias.asname or alias.name.rsplit(".", 1)[-1],),
                )
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            for alias in node.names:
                if alias.name == "*":
                    imports.append(ImportFact(base, node.lineno, symbols=("*",)))
                    continue
                imports.append(
                    ImportFact(
                        _from_import_candidate(base, alias.name),
                        node.lineno,
                        (base,) if base else (),
                        (alias.name,),
                    )
                )

    for statement in module.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            exports.append(statement.name)
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            exports.extend(
                target.id for target in targets if isinstance(target, ast.Name)
            )
    return definitions, imports, exports


def _javascript_facts(
    root: Node, source: bytes
) -> tuple[list[str], list[ImportFact], list[str]]:
    definitions: list[str] = []
    imports: list[ImportFact] = []
    exports: list[str] = []

    def symbols(node: Node) -> tuple[str, ...]:
        statement = _text(node, source)
        prefix = statement.split(" from ", 1)[0]
        names = re.findall(r"[A-Za-z_$][\w$]*", prefix)
        ignored = {"as", "export", "from", "import", "type"}
        return tuple(sorted({name for name in names if name not in ignored}))

    for node in _walk(root):
        if node.type in {
            "function_declaration",
            "class_declaration",
            "method_definition",
        }:
            name = _text(node.child_by_field_name("name"), source)
            if name:
                definitions.append(name)
        elif node.type == "import_statement":
            # `import type {...} from 'x'` is erased at compile time; it is not a
            # runtime dependency, so it must not produce an edge.
            if re.match(r"import\s+type\b", _text(node, source)):
                continue
            module = _text(node.child_by_field_name("source"), source).strip("'\"")
            if module:
                imports.append(
                    ImportFact(module, node.start_point.row + 1, symbols=symbols(node))
                )
        elif node.type == "export_statement":
            declaration = node.child_by_field_name("declaration")
            name = (
                _text(declaration.child_by_field_name("name"), source)
                if declaration
                else ""
            )
            if name:
                exports.append(name)
            if re.match(r"export\s+type\b", _text(node, source)):
                continue
            module = _text(node.child_by_field_name("source"), source).strip("'\"")
            if module:
                imports.append(
                    ImportFact(module, node.start_point.row + 1, symbols=symbols(node))
                )
        elif node.type == "call_expression":
            # Capture both CommonJS require('x') and dynamic import('x'); the
            # latter parses as a call whose function is the `import` keyword.
            function = _text(node.child_by_field_name("function"), source)
            arguments = node.child_by_field_name("arguments")
            if function in {"require", "import"} and arguments:
                strings = [
                    child
                    for child in arguments.named_children
                    if child.type == "string"
                ]
                if strings:
                    imports.append(
                        ImportFact(
                            _text(strings[0], source).strip("'\""),
                            node.start_point.row + 1,
                            symbols=(),
                        )
                    )
    return definitions, imports, exports


def parse_file(path: Path) -> SymbolTable:
    language = classify(path)
    if language is None:
        raise ValueError(f"Unsupported source file: {path}")

    source = path.read_bytes()
    tree = _parser(path, language).parse(source)
    if tree.root_node.has_error:
        raise SyntaxError(f"tree-sitter could not parse {path}")

    if language == "python":
        definitions, imports, exports = _python_facts(path, source)
    else:
        definitions, imports, exports = _javascript_facts(tree.root_node, source)

    return SymbolTable(
        path=path,
        language=language,
        definitions=tuple(sorted(set(definitions))),
        imports=tuple(
            sorted(
                set(imports),
                key=lambda item: (
                    item.line,
                    item.module,
                    item.fallbacks,
                    item.symbols,
                ),
            )
        ),
        exports=tuple(sorted(set(exports))),
        loc=_line_count(source),
    )


def _line_count(source: bytes) -> int:
    # errors="replace" keeps the count identical for valid UTF-8 while not
    # crashing on files that parse via a non-UTF-8 encoding cookie.
    return len(source.decode("utf-8", errors="replace").splitlines())


def unparsable_table(path: Path, language: str) -> SymbolTable:
    """Import-free table for a file the parsers reject; analysis degrades to
    a node without edges instead of aborting the whole run."""
    try:
        source = path.read_bytes()
    except OSError:
        source = b""
    return SymbolTable(
        path=path,
        language=language,
        definitions=(),
        imports=(),
        exports=(),
        loc=_line_count(source),
    )
