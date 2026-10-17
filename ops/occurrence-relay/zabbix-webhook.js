/*
 * Zabbix 7.4 webhook script installed as a private media type for MAESTRO.
 * Provided params by Zabbix Media type: endpoint, secret, event_source, event_id,
 * event_recovery_id, event_ts, recovery_ts, host_id, trigger_id,
 * event_name, severity.
 * No global source/tenant claims and no command execution.
 */
var p = JSON.parse(value);

function identifier(name, maxLen) {
    var v = p[name];
    if (typeof v !== 'string' || !v.length || v.length > maxLen ||
        !/^[A-Za-z0-9_.:-]+$/.test(v)) {
        throw 'INVALID_PARAM_' + name;
    }
    return v;
}

function timestamp(seconds) {
    var n = Number(seconds);
    if (!isFinite(n) || n < 946684800 || n > 4102444800) {
        throw 'INVALID_EVENT_TIMESTAMP';
    }
    return new Date(n * 1000).toISOString();
}

if (p.event_source !== '0') {
    throw 'UNSUPPORTED_EVENT_SOURCE';
}
var eventId = identifier('event_id', 64);
var hostId = identifier('host_id', 64);
var triggerId = identifier('trigger_id', 64);
var recovered = typeof p.event_recovery_id === 'string' &&
    /^[0-9]+$/.test(p.event_recovery_id) && p.event_recovery_id !== '0';

if (typeof p.endpoint !== 'string' || !/^https:\/\//.test(p.endpoint) ||
    p.endpoint.length > 512) {
    throw 'INVALID_ENDPOINT';
}
if (typeof p.secret !== 'string' || p.secret.length < 32 || p.secret.length > 512) {
    throw 'INVALID_AUTH_CONFIGURATION';
}
var sev = Number(p.severity);
if (!/^[0-5]$/.test(String(p.severity)) || !isFinite(sev)) {
    throw 'INVALID_SEVERITY';
}
var eventKind = recovered ? 'RECOVERY' : 'PROBLEM';
var payload = {
    schema_version: '1',
    event_id: recovered ? 'recovery:' + identifier('event_recovery_id', 64) :
        'problem:' + eventId,
    occurred_at: timestamp(recovered ? p.recovery_ts : p.event_ts),
    asset_ref: 'zabbix:host:' + hostId,
    signal_code: 'ZBX_TRIGGER:' + triggerId,
    status: eventKind,
    severity: sev
};
if (recovered) {
    payload.problem_event_id = 'problem:' + eventId;
}
if (typeof p.event_name === 'string' && p.event_name.length) {
    payload.summary = p.event_name.slice(0, 500);
}

var request = new HttpRequest();
request.addHeader('Content-Type: application/json');
request.addHeader('Authorization: Bearer ' + p.secret);
var response = request.post(p.endpoint, JSON.stringify(payload));
var code = request.getStatus();
if (code !== 200 && code !== 201) {
    // Do not copy provider response, token, or incident data into error logs.
    throw 'OCCURRENCE_RELAY_DID_NOT_ACCEPT';
}
return JSON.stringify({
    status: 'accepted',
    event_id: payload.event_id
});
