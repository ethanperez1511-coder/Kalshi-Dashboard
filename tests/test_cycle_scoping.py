"""No name inside the cycle may shadow a module-level import (L27 shape).

2026-10-13 01:37 UTC: the digest's Fees line said "unavailable". The heartbeat
lambda used FeeBook, and a later `from ... import FeeBook` inside the same
function made FeeBook a LOCAL of the whole function, unbound when the
heartbeat ran. NameError, caught by the section wrapper, and shown only as
"unavailable". The import was redundant: the module already imported FeeBook.
"""
import src.run_trading as rt


def _shadowed(fn):
    code = fn.__code__
    return sorted((set(code.co_varnames) | set(code.co_cellvars)) & set(vars(rt)))


def test_the_cycle_shadows_no_module_level_name():
    assert _shadowed(rt._run_pipeline_locked) == []


def test_execution_shadows_no_module_level_name():
    assert _shadowed(rt.execute_qualifying) == []
