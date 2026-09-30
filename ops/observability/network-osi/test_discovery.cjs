const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const parser = fs.readFileSync(path.join(__dirname, 'snmp.js'), 'utf8');
const discovery = new Function('value', parser + fs.readFileSync(path.join(__dirname, 'discovery.js'), 'utf8'));
const classify = new Function('value', parser + fs.readFileSync(path.join(__dirname, 'neighbor.js'), 'utf8') +
  "return neighbor(snmp(value), '42', '192.0.2.2');");
const row = JSON.parse(discovery([
  '.1.3.6.1.2.1.31.1.1.1.1.42 = STRING: "ANDY210-GW"',
  '.1.3.6.1.2.1.31.1.1.1.1.43 = STRING: "ANDY218-GW"',
  '.1.3.6.1.2.1.4.20.1.2.192.0.2.1 = INTEGER: 42',
  '.1.3.6.1.2.1.4.20.1.3.192.0.2.1 = IpAddress: 255.255.255.248',
].join('\n')));
assert.equal(row.length, 1);
assert.equal(row[0]['{#SNMPINDEX}'], '42');
assert.equal(row[0]['{#NETWORK}'], '192.0.2.0/29');
assert.equal(row[0]['{#ENDPOINT}'], '192.0.2.2');
assert.equal(JSON.parse(discovery('.1.3.6.1.2.1.31.1.1.1.1.42 = STRING: "ANDY210-GW"'))[0]['{#ENDPOINT}'], '');
const base = '.1.3.6.1.2.1.4.22.1.';
function arp(mac, type) {
  return base + '2.42.192.0.2.2 = Hex-STRING: ' + mac + '\n' + base + '4.42.192.0.2.2 = INTEGER: ' + type;
}
assert.deepEqual(classify(arp('', '2')), {state: 2, mac: 'UNRESOLVED'});
assert.deepEqual(classify(arp('00 00 00 00 00 00', '3')), {state: 2, mac: 'UNRESOLVED'});
assert.deepEqual(classify(arp('02 00 00 00 00 01', '3')), {state: 1, mac: '02:00:00:00:00:01'});
// RouterOS emits STRING MACs with unpadded octets for both dynamic and static ARP.
for (const type of ['3', '4']) {
  assert.deepEqual(classify(arp('"2:0:a:ff:0:1"', type).replace('Hex-STRING:', 'STRING:')),
    {state: 1, mac: '02:00:0A:FF:00:01'});
}
assert.deepEqual(classify(arp('0:0:0:0:0:0', '4')), {state: 2, mac: 'UNRESOLVED'});
assert.deepEqual(classify(arp('02000AFF0001', '4')), {state: 1, mac: '02:00:0A:FF:00:01'});
assert.equal(classify(arp('2:0:a:ff:0:100', '4')).state, 0);
assert.equal(classify(arp('garbled-02000aff0001', '4')).state, 0);
assert.equal(classify(arp('garbled', '3')).state, 0);
assert.equal(classify(arp('02 00 00 00 00 01', '1')).state, 0);
assert.equal(classify(base + '4.42.192.0.2.2 = INTEGER: 3').state, 0);
assert.equal(classify(base + '2.42.192.0.2.2 = No Such Instance').state, 0);
assert.equal(classify('').state, 0);
assert.equal(classify(base + '4.42.192.0.2.3 = INTEGER: 3').state, 3);
console.log('PASS: LLD addressing and COMPLETE/INCOMPLETE/ABSENT/UNKNOWN fixtures');
