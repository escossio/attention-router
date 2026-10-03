// Parse the numeric OID output produced by Zabbix native walk[] / get[].
// A missing OID remains missing; it is never translated into a healthy value.
function snmp(raw) {
  var out = {};
  raw.split(/\r?\n/).forEach(function (line) {
    var m = line.match(/^\.?([0-9.]+)\s*=\s*(.*)$/);
    if (!m || /No Such|endOfMib|End of MIB/i.test(m[2])) return;
    var value = m[2].replace(/^[A-Za-z0-9_-]+:\s*/, '').trim();
    if (value.charAt(0) === '"' && value.charAt(value.length - 1) === '"') {
      value = value.slice(1, -1);
    }
    out[m[1]] = value;
  });
  return out;
}
