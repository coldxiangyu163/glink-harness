#!/usr/bin/env python3
"""Reproducible disposable SQLite+event-log seed, before starting the local host.

Usage: PYTHONPATH=<main-repo>/host python m9-inject.py <temporary-data-root>
Never point at existing run data. RunStore writes actual runs.db records/events through its
public transactional create/commit_terminal path (not fabricated API responses).
"""
import json
from pathlib import Path
import sys
import time
from pi_glink.store import RunSpec, RunStore

root = Path(sys.argv[1]); assert root.parent.is_dir() and not root.exists(), 'fresh empty data root required'
root.mkdir(mode=0o700)
store = RunStore(root / 'runs')
spec = RunSpec(family='pi', pack='pi', executor='inproc')
chain_indices = (0, 55, 80, 109)
chain = []
try:
    for index in range(110):
        run_id = f'{index + 1:012x}'
        parent = chain[-1] if index in chain_indices[1:] else None
        record = store.create(f'M9 合成历史 {index + 1:03d}', run_id=run_id, spec=spec,
                              parent_run_id=parent, workspace_root=root / 'workspaces')
        store.commit_terminal(record.run_id, {
            'type': 'run_finished', 'runId': run_id, 'seq': 1, 'ts': time.time(),
            'payload': {'status': 'finished', 'cleanup': {'released': True, 'failures': [], 'external': {}}},
        })
        if index in chain_indices:
            chain.append(run_id)
    assert len(store.list(limit=200)) == 110
    assert all(len(list(store.iter_events(item))) == 1 for item in chain)
finally:
    store.close()
print(json.dumps({'injected': 110, 'terminalFrames': 110, 'chain': chain,
                  'expectedTopLevel': 107, 'storage': 'runs/runs.db SQLite events table'}, ensure_ascii=False, indent=2))
