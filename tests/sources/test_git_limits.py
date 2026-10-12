from dataclasses import replace
import sys
import time

import pytest

from oms.sources.limits import AcquisitionLimits, Budget, BudgetExceeded, run_bounded


def test_concrete_defaults_and_invalid_limits():
    limits = AcquisitionLimits()
    assert (limits.operation_seconds, limits.command_seconds, limits.refs, limits.history_commits) == (300, 60, 10000, 4096)
    with pytest.raises(ValueError):
        replace(limits, operation_seconds=float('inf'))
    with pytest.raises(ValueError):
        replace(limits, files=0)


def test_streamed_output_cap_terminates_child(tmp_path):
    budget = Budget(replace(AcquisitionLimits(), stdout_bytes=1024))
    with pytest.raises(BudgetExceeded, match='stdout'):
        run_bounded([sys.executable, '-c', "import os; os.write(1,b'x'*1000000)"], {}, tmp_path, budget)


def test_command_deadline_terminates_child(tmp_path):
    budget = Budget(replace(AcquisitionLimits(), command_seconds=0.1))
    start = time.monotonic()
    with pytest.raises(BudgetExceeded, match='deadline'):
        run_bounded([sys.executable, '-c', 'import time; time.sleep(10)'], {}, tmp_path, budget)
    assert time.monotonic() - start < 2


def test_cache_growth_and_expansion_are_cumulative(tmp_path):
    budget = Budget(replace(AcquisitionLimits(), acquired_bytes=5, expanded_bytes=8))
    budget.watch_cache(tmp_path)
    (tmp_path / 'pack').write_bytes(b'123456')
    with pytest.raises(BudgetExceeded, match='acquired'):
        budget.check()
    fresh = Budget(replace(AcquisitionLimits(), expanded_bytes=8))
    fresh.expand(5)
    with pytest.raises(BudgetExceeded, match='expanded'):
        fresh.expand(4)


def test_stderr_is_bounded_and_failure_diagnostics_are_not_returned(tmp_path):
    budget = Budget(replace(AcquisitionLimits(), stderr_bytes=32))
    with pytest.raises(BudgetExceeded, match='stderr'):
        run_bounded([sys.executable, '-c', "import os; os.write(2,b'x'*1000)"], {}, tmp_path, budget)


def test_cancelled_operation_never_starts_child(tmp_path):
    marker = tmp_path / 'started'
    budget = Budget(AcquisitionLimits(), cancelled=lambda: True)
    with pytest.raises(BudgetExceeded, match='cancelled'):
        run_bounded([sys.executable, '-c', f'open({str(marker)!r}, "w").close()'], {}, tmp_path, budget)
    assert not marker.exists()


def test_tenant_and_process_concurrency_are_bounded(tmp_path):
    from oms.sources.limits import AcquisitionLease
    limits = replace(AcquisitionLimits(), per_tenant=1, per_process=2)
    with AcquisitionLease(tmp_path, 'tenant', 'first', limits):
        with pytest.raises(BudgetExceeded):
            with AcquisitionLease(tmp_path, 'tenant', 'second', limits):
                pass
        with AcquisitionLease(tmp_path, 'other', 'third', limits):
            with pytest.raises(BudgetExceeded):
                with AcquisitionLease(tmp_path, 'third', 'fourth', limits):
                    pass
    with AcquisitionLease(tmp_path, 'tenant', 'first', limits):
        pass


@pytest.mark.parametrize('condition', ['cache_growth', 'cancelled'])
def test_closed_output_pipes_do_not_stop_budget_monitoring(tmp_path, condition):
    marker = tmp_path / 'pack'
    limits = replace(AcquisitionLimits(), command_seconds=2, acquired_bytes=16)
    budget = Budget(limits, cancelled=lambda: condition == 'cancelled' and marker.exists())
    budget.watch_cache(tmp_path)
    script = "import os,time; os.close(1); os.close(2); time.sleep(.1); open('pack','wb').write(b'x'*32); time.sleep(5)"
    start = time.monotonic()
    with pytest.raises(BudgetExceeded, match='acquired' if condition == 'cache_growth' else 'cancelled'):
        run_bounded([sys.executable, '-c', script], {}, tmp_path, budget)
    assert time.monotonic() - start < 1
