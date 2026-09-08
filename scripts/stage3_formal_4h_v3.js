const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

function arg(name, fallback = null) {
  const idx = process.argv.indexOf(name);
  if (idx === -1) return fallback;
  const value = process.argv[idx + 1];
  return value && !value.startsWith('--') ? value : fallback;
}

function hasFlag(name) {
  return process.argv.includes(name);
}

function run(cmd, args, options = {}) {
  try {
    return execFileSync(cmd, args, {
      encoding: 'utf8',
      timeout: options.timeout || 8000,
      maxBuffer: 1024 * 1024,
      stdio: ['ignore', 'pipe', 'pipe'],
    }).trim();
  } catch {
    return null;
  }
}

function csvCell(value) {
  const text = value === null || value === undefined ? '' : String(value);
  return `"${text.replace(/"/g, '""')}"`;
}

function countLines(text) {
  if (!text) return 0;
  return text.split('\n').filter((line) => line.trim()).length;
}

function getInternalIngress() {
  const raw = run('docker', [
    'inspect',
    'attention-router-internal-ingress-1',
    '--format',
    '{{.State.Running}}|{{.State.Health.Status}}|{{.RestartCount}}|{{.Image}}|{{.State.Pid}}',
  ]);
  if (!raw) {
    return {
      internal_ingress_running: null,
      internal_ingress_health: null,
      internal_ingress_restart: null,
      internal_ingress_image: null,
      internal_ingress_pid: null,
    };
  }
  const [running, health, restart, image, pid] = raw.split('|');
  return {
    internal_ingress_running: running,
    internal_ingress_health: health,
    internal_ingress_restart: restart,
    internal_ingress_image: image,
    internal_ingress_pid: pid,
  };
}

function getContainerProcMetrics(container) {
  const top = run('docker', ['top', container, '-eo', 'pid']);
  const count = top ? Math.max(0, top.split('\n').filter((line, index) => index > 0 && line.trim()).length) : null;
  const raw = run('docker', [
    'exec',
    container,
    'sh',
    '-lc',
    "main_fd=$(ls /proc/1/fd 2>/dev/null | wc -l); total_fd=$(find /proc/[0-9]*/fd -type l 2>/dev/null | wc -l); tcp_total=$(awk 'NR>1{n++} END{print n+0}' /proc/net/tcp /proc/net/tcp6 2>/dev/null); close_wait=$(awk 'NR>1 && $4==\"08\"{n++} END{print n+0}' /proc/net/tcp /proc/net/tcp6 2>/dev/null); printf \"%s|%s|%s|%s\\n\" \"$main_fd\" \"$total_fd\" \"$tcp_total\" \"$close_wait\"",
  ]);
  if (!raw) {
    return {
      process_count: count,
      main_pid_fd: null,
      total_fd: null,
      tcp_total: null,
      close_wait: null,
    };
  }
  const [main_pid_fd, total_fd, tcp_total, close_wait] = raw.split('|');
  return { process_count: count, main_pid_fd, total_fd, tcp_total, close_wait };
}

function getDatabaseMetrics() {
  const queries = {
    postgres_idle_in_transaction: "SELECT count(*) FROM pg_stat_activity WHERE state='idle in transaction';",
    postgres_blockers: "SELECT count(*) FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;",
    postgres_blocked: "SELECT count(*) FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;",
    postgres_transactionid_waits: "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' AND wait_event='transactionid';",
    duplicate_count: "SELECT count(*) FROM (SELECT source, external_event_id FROM inbound_events GROUP BY 1,2 HAVING count(*) > 1) d;",
    outbox_active: "SELECT count(*) FROM outbox_messages WHERE status IN ('PENDING','RETRY','PROCESSING','SENDING');",
    external_outbox_active: "SELECT count(*) FROM outbox_messages WHERE status IN ('PENDING','RETRY','PROCESSING','SENDING') AND destination <> 'wwebjs';",
    inbound_events_count: "SELECT count(*) FROM inbound_events;",
    interactions_count: "SELECT count(*) FROM interactions;",
  };
  const result = {};
  for (const [key, query] of Object.entries(queries)) {
    let value = null;
    const raw = run('docker', ['exec', '-i', 'attention-router-db-1', 'psql', '-U', 'attention_router', '-d', 'attention_router', '-Atc', query]);
    value = raw && /^\d+$/.test(raw) ? raw : null;
    result[key] = value;
  }
  return result;
}

function getHaStatus() {
  const raw = run('curl', ['-fsS', 'http://192.0.2.7:18181/status'], { timeout: 8000 });
  if (!raw) {
    return {
      HA_health: null,
      HA_spool_pending: null,
      HA_outbound_pending: null,
    };
  }
  try {
    const parsed = JSON.parse(raw);
    return {
      HA_health: parsed.ready ? 'READY' : parsed.state || null,
      HA_spool_pending: parsed.spool_pending ?? null,
      HA_outbound_pending: parsed.outbound_pending ?? null,
    };
  } catch {
    return {
      HA_health: null,
      HA_spool_pending: null,
      HA_outbound_pending: null,
    };
  }
}

function sample() {
  const now = new Date().toISOString();
  const ingress = getInternalIngress();
  const proc = getContainerProcMetrics('attention-router-internal-ingress-1');
  const db = getDatabaseMetrics();
  const ha = getHaStatus();
  return {
    timestamp: now,
    ...ingress,
    ...proc,
    ...db,
    ...ha,
  };
}

function header() {
  return [
    'timestamp',
    'internal_ingress_running',
    'internal_ingress_health',
    'internal_ingress_restart',
    'internal_ingress_image',
    'process_count',
    'main_pid_fd',
    'total_fd',
    'tcp_total',
    'close_wait',
    'postgres_idle_in_transaction',
    'postgres_blockers',
    'postgres_blocked',
    'postgres_transactionid_waits',
    'duplicate_count',
    'outbox_active',
    'external_outbox_active',
    'HA_health',
    'HA_spool_pending',
    'HA_outbound_pending',
    'inbound_events_count',
    'interactions_count',
  ];
}

async function main() {
  const output = arg('--output', path.join('/tmp/attention-router-internal-ingress-formal-4h-20260813', 'collector.csv'));
  const intervalMs = Number(arg('--interval-ms', '60000'));
  const maxSamples = hasFlag('--max-samples') ? Number(arg('--max-samples', '0')) : 0;
  fs.mkdirSync(path.dirname(output), { recursive: true, mode: 0o700 });
  const needsHeader = !fs.existsSync(output);
  if (needsHeader) {
    fs.writeFileSync(output, `${header().join(',')}\n`, { mode: 0o600 });
  }
  let count = 0;
  while (true) {
    const row = sample();
    const line = header().map((key) => csvCell(row[key])).join(',');
    fs.appendFileSync(output, `${line}\n`, { mode: 0o600 });
    count += 1;
    if (maxSamples > 0 && count >= maxSamples) break;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

main().catch((error) => {
  console.error(String(error?.stack || error));
  process.exit(1);
});
