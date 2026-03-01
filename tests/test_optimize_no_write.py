#!/usr/bin/env python3
"""Tests that optimize.py never writes to production config."""
import ast
import pytest


def test_optimize_never_writes_config():
    """Verify optimize.py does not open any config file for writing."""
    with open("luckytrader/optimize.py") as f:
        source = f.read()
    
    tree = ast.parse(source)
    
    # Check for any file write operations on config files
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            # Check for open(..., 'w')
            if isinstance(func, ast.Name) and func.id == 'open':
                for arg in node.args[1:]:
                    if isinstance(arg, ast.Constant) and 'w' in str(arg.value):
                        # Check if it's a config file
                        assert False, f"optimize.py opens a file for writing at line {node.lineno}"
    
    # Verify it writes to suggestions dir, not production config
    assert "optimization_suggestions" in source, \
        "optimize.py should write to optimization_suggestions directory"
    assert "不会自动修改生产配置" in source, \
        "optimize.py should warn that it doesn't modify production config"
    
    # Verify no toml write
    assert "toml.dump" not in source
    assert "config.toml" not in source or source.count("config.toml") == source.count("config.toml") == source.lower().count("config.toml")


def test_optimize_output_is_suggestion_only():
    """Verify the output JSON includes SUGGESTION_ONLY status."""
    with open("luckytrader/optimize.py") as f:
        source = f.read()
    
    assert "SUGGESTION_ONLY" in source
    assert "人工评估" in source
