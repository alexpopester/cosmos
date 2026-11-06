"""Contracts between parts that must agree (EVALUATION-2026-11-05 B49).

- The node registries: the editor's nodeTypes.js, the engine's graph.NODE_TYPES and runner.EXECUTORS name the
  same node types, with the same output handles and error ports (CLAUDE.md: keep them in sync).
- The backends: FakeBackend (tests) has every method CosmosBackend has, taking the same arguments, so a test
  can't pass against a method COSMOS doesn't get.
"""

import inspect
import os
import re

from conftest import FakeBackend
from flow_automator.cosmos_backend import CosmosBackend
from flow_automator.graph import NODE_TYPES
from flow_automator.runner import EXECUTORS


NODE_TYPES_JS = os.path.join(os.path.dirname(__file__), "..", "src", "tools", "flowautomator", "nodeTypes.js")


def editor_registry():
    """{type: {"outputs": [...] or "dynamic", "errors": bool}} from nodeTypes.js (plain data, two-space keys)."""
    with open(NODE_TYPES_JS) as file:
        text = file.read()
    body = text[text.index("export const NODE_TYPES = {") :]
    blocks = re.split(r"\n  (\w+): \{\n", body)[1:]
    registry = {}
    for name, block in zip(blocks[::2], blocks[1::2], strict=True):
        block = block.split("\n  },")[0]  # this type's own lines
        outputs = re.search(r"\n    outputs: (\[[^\]]*\]|\(.*)", block)
        assert outputs, f"{name}: no outputs in nodeTypes.js"
        listed = outputs.group(1)
        registry[name] = {
            "outputs": re.findall(r"'([^']+)'", listed) if listed.startswith("[") else "dynamic",
            "errors": "\n    errors: true," in block,
        }
    return registry


def test_the_editor_and_the_engine_know_the_same_node_types():
    editor = editor_registry()
    assert set(editor) == set(NODE_TYPES) == set(EXECUTORS), {
        "editor only": set(editor) - set(NODE_TYPES),
        "engine only": set(NODE_TYPES) - set(editor),
        "no executor": set(NODE_TYPES) - set(EXECUTORS),
    }


def test_output_handles_and_error_ports_match():
    editor = editor_registry()
    for name, spec in NODE_TYPES.items():
        outputs = spec["outputs"]
        if callable(outputs):
            assert editor[name]["outputs"] == "dynamic", (
                f"{name}: the engine computes its outputs, the editor lists them"
            )
        else:
            assert editor[name]["outputs"] == list(outputs), f"{name}: outputs differ"
        assert editor[name]["errors"] == bool(spec.get("errors")), f"{name}: error port differs"


def test_the_switch_cases_agree():
    # The one dynamic node: case_<i> per case, then default, in both
    assert NODE_TYPES["switch"]["outputs"]({"cases": ["a", "b"]}) == ["case_0", "case_1", "default"]
    with open(NODE_TYPES_JS) as file:
        assert "`case_${i}`), 'default']" in file.read()


def public_methods(cls):
    return {
        name: inspect.signature(member)
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def test_the_fake_backend_has_every_cosmos_backend_method():
    cosmos, fake = public_methods(CosmosBackend), public_methods(FakeBackend)
    missing = sorted(set(cosmos) - set(fake))
    assert not missing, f"FakeBackend lacks {missing}: tests would never call them"
    for name, signature in cosmos.items():
        expected = [p for p in signature.parameters if p != "self"]
        got = [p for p in fake[name].parameters if p != "self"]
        assert got[: len(expected)] == expected, f"{name}: CosmosBackend{signature} vs FakeBackend{fake[name]}"
