"""Regression guard: live own-reply scan must invoke Task 2A/2B classification.

Task 2A/2B can pass unit tests while remaining dead code if the production
`own-replies scan --propose` path never calls `classify_reply`. This guard
keeps the live watchdog wired to the context/intent/DM-opportunity pipeline.
"""

from pathlib import Path
import ast

REPO = Path(__file__).resolve().parent.parent
SOURCE = (REPO / "src" / "threads_operator" / "operator_cli.py").read_text()


def test_live_ownreply_scan_calls_classify_reply():
    tree = ast.parse(SOURCE)
    target = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_run_ownreply_scan"
    )
    calls = [
        node for node in ast.walk(target)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "classify_reply"
    ]
    assert calls, "_run_ownreply_scan must call own_replies.classify_reply()"
