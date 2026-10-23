"""Local runtime persistence boundary, rotation, retention and degraded I/O.

All records and secret-like values are synthetic. All disk writes use tmp_path.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
import subprocess
import sys

import pytest

from backend.logging_audit import runtime_sink
from backend.logging_audit.logger import StructuredLogger
from backend.logging_audit.runtime_sink import RuntimeSink

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def record(index=0):
    return {"schema_version": 1, "timestamp": NOW.isoformat(), "level": "WARN",
            "event": "model.call.failed", "service": "api", "environment": "test",
            "stage": "generate", "status": "failed", "duration_ms": index,
            "error_code": "OUTPUT_INVALID", "task_id": f"synthetic_{index}"}


def rows(directory):
    return [json.loads(line) for p in sorted(directory.glob('runtime-*.jsonl'))
            for line in p.read_text(encoding='utf-8').splitlines()]


@pytest.fixture
def default_sink(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_LOG_DIR", str(tmp_path / 'runtime'))
    runtime_sink._sink.cache_clear()
    yield tmp_path / 'runtime'
    runtime_sink._sink.cache_clear()


def test_default_logger_persists_sanitized_record(default_sink, capsys):
    out = StructuredLogger('api', 'test').log('model.call.failed', stage='generate',
        status='failed', error_code='OUTPUT_INVALID', duration_ms=1,
        reason='fake@synthetic.test sk-syntheticcredential https://synthetic.test/?key=fake\nnext')
    assert rows(default_sink) == [out]
    text = next(default_sink.glob('*.jsonl')).read_text(encoding='utf-8')
    assert 'fake@synthetic.test' not in text and 'sk-syntheticcredential' not in text
    assert '?key=' not in text and len(text.splitlines()) == 1
    assert json.loads(capsys.readouterr().out) == out


def test_explicit_stream_never_opens_default_disk(default_sink):
    stream = io.StringIO()
    StructuredLogger('api', stream=stream).log('model.call.completed', stage='generate', status='succeeded', duration_ms=1)
    assert json.loads(stream.getvalue())['event'] == 'model.call.completed'
    assert not default_sink.exists()


@pytest.mark.parametrize('separate_channel', [True, False])
def test_content_never_creates_runtime_sink(default_sink, separate_channel, capsys):
    content = io.StringIO() if separate_channel else None
    StructuredLogger('api', content_stream=content).log('model.call.content', stage='generate',
        status='succeeded', model_answer='synthetic private content')
    assert not default_sink.exists()
    text = content.getvalue() if content is not None else capsys.readouterr().out
    assert json.loads(text)['model_answer'] == 'synthetic private content'


def test_disk_sink_rechecks_content_boundary(tmp_path):
    sink = RuntimeSink(tmp_path)
    content = record() | {'event': 'model.call.content', 'model_answer': 'synthetic'}
    assert sink.append(content) is False
    assert sink.append(record() | {'model_prompt': 'synthetic'}) is False
    assert not list(tmp_path.iterdir())


def test_non_runtime_audit_not_duplicated_on_disk(default_sink, capsys):
    StructuredLogger('api').log('audit.query.executed', actor_id='synthetic_admin', role='admin',
                               result='succeeded', query_id='synthetic_query')
    assert not default_sink.exists()
    assert json.loads(capsys.readouterr().out)['event'] == 'audit.query.executed'


def test_size_rotation_keeps_complete_utf8_lines_and_directory_bound(tmp_path):
    sink = RuntimeSink(tmp_path, max_file_bytes=700, max_files=3, clock=lambda: NOW, pid=11)
    for i in range(25):
        assert sink.append(record(i))
        files = list(tmp_path.glob('*.jsonl'))
        assert len(files) <= 3 and all(p.stat().st_size <= 700 for p in files)
    assert len(rows(tmp_path)) >= 1
    assert rows(tmp_path)[-1]['task_id'] == 'synthetic_24'
    assert sink.snapshot()['pruned'] > 0


def test_same_pid_restart_appends_and_new_process_uses_separate_file(tmp_path):
    first = RuntimeSink(tmp_path, clock=lambda: NOW, pid=11)
    assert first.append(record(1))
    restart = RuntimeSink(tmp_path, clock=lambda: NOW, pid=11)
    assert restart.append(record(2))
    assert len(list(tmp_path.glob('*.jsonl'))) == 1
    other = RuntimeSink(tmp_path, clock=lambda: NOW, pid=12)
    assert other.append(record(3))
    assert len(list(tmp_path.glob('*.jsonl'))) == 2
    assert [r['duration_ms'] for r in rows(tmp_path)] == [1, 2, 3]


def test_retention_day_rollover_does_not_touch_legacy_or_unmanaged_files(tmp_path):
    stale = tmp_path / f"runtime-{(NOW-timedelta(days=14)):%Y%m%d}-p1-000000.jsonl"
    recent = tmp_path / f"runtime-{(NOW-timedelta(days=13)):%Y%m%d}-p1-000000.jsonl"
    stale.write_text('stale', encoding='utf-8'); recent.write_text(json.dumps(record())+'\n',encoding='utf-8')
    legacy = tmp_path / 'uvicorn.out.log'; legacy.write_text('synthetic raw stdout',encoding='utf-8')
    other = tmp_path / 'runtime-unmanaged.jsonl'; other.write_text('leave',encoding='utf-8')
    clock = [NOW]
    sink = RuntimeSink(tmp_path, clock=lambda: clock[0])
    assert sink.append(record())
    assert not stale.exists() and recent.exists()
    clock[0] += timedelta(days=1)
    assert sink.append(record(1))
    assert not recent.exists()
    assert legacy.read_text(encoding='utf-8') == 'synthetic raw stdout'
    assert other.read_text(encoding='utf-8') == 'leave'
    assert len(list(tmp_path.glob(f'runtime-{NOW:%Y%m%d}-*.jsonl'))) == 1
    assert len(list(tmp_path.glob(f'runtime-{clock[0]:%Y%m%d}-*.jsonl'))) == 1


def test_concurrent_loggers_share_lock_and_do_not_lose_lines(tmp_path):
    sinks = [RuntimeSink(tmp_path, clock=lambda: NOW, pid=11) for _ in range(5)]
    def emit(i):return sinks[i % 5].append(record(i))
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert all(pool.map(emit, range(300)))
    stored = rows(tmp_path)
    assert len(stored) == 300
    assert {r['task_id'] for r in stored} == {f'synthetic_{i}' for i in range(300)}


def test_io_failure_is_counted_rate_limited_and_does_not_leak_path(tmp_path, capsys):
    blocked = tmp_path / 'synthetic-secret-directory'
    blocked.write_text('not directory',encoding='utf-8')
    ticks = [0.0]
    sink = RuntimeSink(blocked, monotonic=lambda:ticks[0])
    for i in range(4): assert sink.append(record(i)) is False
    warning = capsys.readouterr().err
    assert len(warning.splitlines()) == 1
    assert 'synthetic-secret-directory' not in warning and str(tmp_path) not in warning
    alert = json.loads(warning)
    assert alert['event'] == 'observability.export.failed' and alert['exporter'] == 'local_runtime'
    assert sink.snapshot()['io_failures'] == 4 and sink.snapshot()['dropped'] == 4
    ticks[0] = 61
    assert sink.append(record()) is False
    assert json.loads(capsys.readouterr().err)['dropped_count'] == 5


def test_default_logger_keeps_returning_when_disk_is_unwritable(default_sink, capsys):
    default_sink.write_text('blocking file',encoding='utf-8')
    out = StructuredLogger('api').log('model.call.completed', stage='generate', status='succeeded',duration_ms=1)
    assert out['status'] == 'succeeded'
    assert runtime_sink.runtime_sink_snapshot()['io_failures'] == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == out
    assert json.loads(captured.err)['error_code'] == 'INTERNAL_ERROR'


def test_broken_stdout_does_not_prevent_disk_persistence(default_sink, monkeypatch):
    class Broken(io.StringIO):
        def write(self, value):raise OSError('synthetic raw sensitive stdout error')
    monkeypatch.setattr(sys,'stdout',Broken())
    out = StructuredLogger('api').log('model.call.completed',stage='generate',status='succeeded',duration_ms=1)
    assert rows(default_sink) == [out]
    assert runtime_sink.runtime_sink_snapshot()['io_failures'] == 1


def test_oversized_record_dropped_instead_of_exceeding_limit(tmp_path,capsys):
    sink=RuntimeSink(tmp_path,max_file_bytes=32)
    assert sink.append(record()) is False
    assert not list(tmp_path.glob('*.jsonl'))
    assert sink.snapshot()['oversized'] == 1
    assert sink.snapshot()['io_failures'] == 0
    assert 'synthetic_0' not in capsys.readouterr().err


def test_warning_failure_never_raises_or_reenters_sink(tmp_path,monkeypatch):
    class Broken(io.StringIO):
        def write(self,value):raise OSError('synthetic secret')
    monkeypatch.setattr(sys,'stderr',Broken())
    sink=RuntimeSink(tmp_path,max_file_bytes=32)
    assert sink.append(record()) is False
    assert sink.snapshot()['warning_failures'] == 1


def test_append_failure_recovers_without_raw_error_or_payload(tmp_path,monkeypatch,capsys):
    from pathlib import Path
    sink=RuntimeSink(tmp_path,clock=lambda:NOW)
    original=Path.open
    def fail_write(path,*args,**kwargs):
        if args and args[0]=='ab':raise OSError('synthetic sensitive error with path')
        return original(path,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'open',fail_write)
        assert sink.append(record()) is False
    assert sink.append(record(1))
    assert sink.snapshot()['io_failures']==1 and len(rows(tmp_path))==1
    warning=capsys.readouterr().err
    assert 'sensitive' not in warning and 'synthetic_0' not in warning


def test_retention_cleanup_failure_is_counted_and_bounded(tmp_path,monkeypatch,capsys):
    from pathlib import Path
    stale=tmp_path/f"runtime-{(NOW-timedelta(days=14)):%Y%m%d}-p1-000000.jsonl"
    stale.write_text('stale',encoding='utf-8')
    sink=RuntimeSink(tmp_path,clock=lambda:NOW)
    original=Path.unlink
    def fail_cleanup(path,*args,**kwargs):
        if path==stale:raise PermissionError('synthetic raw cleanup path')
        return original(path,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'unlink',fail_cleanup)
        assert sink.append(record()) is False
    assert sink.snapshot()['io_failures']==1
    assert sink.append(record(1)) and not stale.exists()
    assert 'cleanup path' not in capsys.readouterr().err


def test_production_default_directory_does_not_follow_launch_cwd(tmp_path):
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    script="import sys;sys.path.insert(0,sys.argv[1]);from app.logging_audit.runtime_sink import default_runtime_directory;print(default_runtime_directory())"
    env=__import__('os').environ.copy()
    env.pop('RUNTIME_LOG_DIR',None)
    result=subprocess.run([sys.executable,'-c',script,str(root/'server'/'backend')],
                          env=env,cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode==0
    assert Path(result.stdout.strip())==root/'server'/'backend'/'.runtime-logs'


def test_process_isolated_output_files(tmp_path):
    # Exercise two real child interpreters, not just a mocked pid.
    root = str(__import__('pathlib').Path(__file__).resolve().parent/'support')
    script = "import sys;sys.path.insert(0,sys.argv[1]);from backend.logging_audit.logger import StructuredLogger;StructuredLogger('api').log('model.call.completed',stage='generate',status='succeeded',duration_ms=1)"
    env = __import__('os').environ.copy() | {'RUNTIME_LOG_DIR':str(tmp_path)}
    for _ in range(2):
        completed=subprocess.run([sys.executable,'-c',script,root],env=env,capture_output=True,text=True)
        assert completed.returncode == 0
    assert len(list(tmp_path.glob('runtime-*.jsonl'))) == 2
    assert len(rows(tmp_path)) == 2
