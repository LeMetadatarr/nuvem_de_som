"""The root conftest leaves tests_arrkit out only where arrkit cannot be installed (Python before 3.12)."""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_tests_arrkit_is_collected_from_python_3_12_and_left_out_before():
    spec = importlib.util.spec_from_file_location("root_conftest", os.path.join(ROOT, "conftest.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.collect_ignore == (["tests_arrkit"] if sys.version_info < (3, 12) else [])
