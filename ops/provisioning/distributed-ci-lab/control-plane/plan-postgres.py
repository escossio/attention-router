#!/usr/bin/env python3
import argparse
import collections
import datetime as dt
import json
import pathlib
import re

parser = argparse.ArgumentParser()
parser.add_argument('--log', required=True)
parser.add_argument('--capacity', required=True)
parser.add_argument('--output-dir', required=True)
parser.add_argument('--source-sha', required=True)
args = parser.parse_args()

log = pathlib.Path(args.log).read_text(errors='replace')
capacity = json.loads(pathlib.Path(args.capacity).read_text())
rx = re.compile(r'^(\d+(?:\.\d+)?)s\s+(setup|call|teardown)\s+(.+)$', re.M)
tests = collections.defaultdict(float)
for sec, phase, nodeid in rx.findall(log):
    tests[nodeid.strip()] += float(sec)
if not tests:
    raise SystemExit('no pytest duration records found')

files = collections.defaultdict(float)
counts = collections.Counter()
for nodeid, seconds in tests.items():
    path = nodeid.split('::', 1)[0]
    files[path] += seconds
    counts[path] += 1

full = capacity['pytest_full_seconds']
fastest = min(full.values())
speeds = {worker: fastest / seconds for worker, seconds in full.items()}
loads = {worker: 0.0 for worker in speeds}
shards = {worker: [] for worker in speeds}

for path, weight in sorted(files.items(), key=lambda item: item[1], reverse=True):
    worker = min(speeds, key=lambda name: (loads[name] + weight) / speeds[name])
    shards[worker].append(path)
    loads[worker] += weight

out = pathlib.Path(args.output_dir)
out.mkdir(parents=True, exist_ok=True)
for worker, paths in shards.items():
    (out / f'postgres-{worker}.txt').write_text('\n'.join(paths) + '\n')
(out / 'postgres-files.txt').write_text('\n'.join(sorted(files)) + '\n')

manifest = {
    'schema_version': 1,
    'source_sha': args.source_sha,
    'generated_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
    'profile_test_count': len(tests),
    'profile_file_count': len(files),
    'profile_accounted_seconds': round(sum(tests.values()), 6),
    'worker_speed_relative': speeds,
    'worker_profile_load_seconds': loads,
    'worker_predicted_seconds': {w: loads[w] / speeds[w] for w in speeds},
    'shard_file_count': {w: len(shards[w]) for w in speeds},
    'file_weights_seconds': dict(sorted(files.items(), key=lambda item: item[1], reverse=True)),
    'file_test_counts': dict(counts),
}
(out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')

print(json.dumps({
    'tests': len(tests),
    'files': len(files),
    'predicted_seconds': {w: round(manifest['worker_predicted_seconds'][w], 2) for w in speeds},
    'shard_file_count': manifest['shard_file_count'],
}, indent=2))
