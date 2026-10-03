// Legacy ipNetToMedia: 0 UNKNOWN, 1 COMPLETE, 2 INCOMPLETE, 3 ABSENT.
function neighbor(o, index, address) {
  var key = index + '.' + address, base = '1.3.6.1.2.1.4.22.1.';
  var mac = o[base + '2.' + key], type = o[base + '4.' + key];
  if (mac === undefined && type === undefined) {
    // No exported ARP table is missing telemetry, not proof of an absent neighbor.
    var exported = Object.keys(o).some(function(k) {
      return k.indexOf(base + '2.') === 0 || k.indexOf(base + '4.') === 0;
    });
    return {state: exported ? 3 : 0, mac: exported ? 'ABSENT' : 'UNKNOWN'};
  }
  if (mac === undefined || type === undefined) return {state: 0, mac: 'UNKNOWN'};
  // RouterOS STRING octets may omit leading zeros; Hex-STRING uses padded octets.
  var text = mac.trim(), hex = null;
  if (/^[a-fA-F0-9]{1,2}(:[a-fA-F0-9]{1,2}){5}$/.test(text)) {
    hex = text.split(':').map(function(octet) { return ('0' + octet).slice(-2); }).join('');
  } else if (/^[a-fA-F0-9]{2}(\s+[a-fA-F0-9]{2}){5}$/.test(text) || /^[a-fA-F0-9]{12}$/.test(text)) {
    hex = text.replace(/\s+/g, '');
  }
  var resolved = hex !== null && !/^0+$/.test(hex);
  var state = text === '' || (hex !== null && /^0+$/.test(hex)) || type === '2' ? 2 :
    (resolved && (type === '3' || type === '4') ? 1 : 0);
  return {state: state, mac: resolved ? hex.toUpperCase().match(/../g).join(':') : (state === 0 ? 'UNKNOWN' : 'UNRESOLVED')};
}
