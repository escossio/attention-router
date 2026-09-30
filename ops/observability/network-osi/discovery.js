var o = snmp(value), rows = [];
var names = '1.3.6.1.2.1.31.1.1.1.1.';
var addressIndex = '1.3.6.1.2.1.4.20.1.2.';
Object.keys(o).forEach(function (oid) {
  if (oid.indexOf(names) !== 0 || !/^ANDY21[0-7]-GW$/.test(o[oid])) return;
  var index = oid.slice(names.length), name = o[oid];
  var row = {'{#SNMPINDEX}': index, '{#IFNAME}': name,
    '{#VLAN}': name.match(/ANDY([0-9]+)-GW/)[1],
    '{#GATEWAY}': '', '{#NETWORK}': '', '{#ENDPOINT}': ''};
  Object.keys(o).forEach(function (ipOid) {
    if (ipOid.indexOf(addressIndex) !== 0 || o[ipOid] !== index) return;
    var ip = ipOid.slice(addressIndex.length);
    if (o['1.3.6.1.2.1.4.20.1.3.' + ip] !== '255.255.255.248') return;
    var parts = ip.split('.').map(Number), last = parts[3];
    // This template admits /29 runtime gateways at the first usable address.
    if (last % 8 !== 1) return;
    row['{#GATEWAY}'] = ip;
    parts[3] = last - 1; row['{#NETWORK}'] = parts.join('.') + '/29';
    parts[3] = last + 1; row['{#ENDPOINT}'] = parts.join('.');
  });
  rows.push(row);
});
rows.sort(function (a, b) { return Number(a['{#VLAN}']) - Number(b['{#VLAN}']); });
return JSON.stringify(rows);
