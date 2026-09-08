'use strict';

const { canonicalIdentity, serializedId, kindForId } = require('./message-id');

function digits(value) {
  return String(value || '').replace(/\D/g, '');
}

function canonicalPhoneIdentity(value, { defaultCountryCode = '55' } = {}) {
  const serialized = canonicalIdentity(value);
  if (!serialized) return null;
  if (serialized.includes('@lid')) return null;

  const user = serialized.split('@', 1)[0];
  let number = digits(user);
  if (number.startsWith('00')) number = number.slice(2);
  if (number.startsWith(defaultCountryCode) && number.length >= 12) {
    if (defaultCountryCode === '55' && number.length === 12) {
      return `+${number.slice(0, 4)}9${number.slice(4)}`;
    }
    return `+${number}`;
  }
  if (number.length === 10 || number.length === 11) {
    return `+${defaultCountryCode}${number}`;
  }
  return null;
}

function authenticatedSelfAliases({ raw, pn, lid } = {}) {
  const wid = canonicalIdentity(raw);
  const exactPn = canonicalIdentity(pn);
  const exactLid = canonicalIdentity(lid);
  const authoritativeWid = wid?.endsWith('@c.us') || wid?.endsWith('@lid') ? wid : null;
  const authoritativePn = exactPn?.endsWith('@c.us') ? exactPn : null;
  const authoritativeLid = exactLid?.endsWith('@lid') ? exactLid : null;
  return {
    wid,
    pn: authoritativePn,
    lid: authoritativeLid,
    aliases: [...new Set([authoritativeWid, authoritativePn, authoritativeLid].filter(Boolean))].sort(),
  };
}

function resolveWhatsAppIdentity({ raw, pn, lid } = {}) {
  const selfIdentity = authenticatedSelfAliases({ raw, pn, lid });
  const rawValue = serializedId(raw) || serializedId(pn) || serializedId(lid);
  const rawKind = kindForId(rawValue);
  const resolvedPn = canonicalPhoneIdentity(pn) || canonicalPhoneIdentity(raw);
  if (resolvedPn) {
    return {
      ...selfIdentity,
      result: 'RESOLVED',
      rawKind: rawKind === 'unknown' ? kindForId(pn) : rawKind,
      rawIdentity: rawValue,
      canonicalPhone: resolvedPn,
      resolutionSource: canonicalPhoneIdentity(pn) ? 'SELF_PN' : 'CLIENT_INFO_WID',
    };
  }
  if (canonicalIdentity(lid)?.includes('@lid') || rawKind === 'lid') {
    return {
      ...selfIdentity,
      result: 'UNRESOLVED',
      rawKind: 'lid',
      rawIdentity: rawValue,
      canonicalPhone: null,
      resolutionSource: 'LID_WITHOUT_AUTHORITATIVE_PN_MAPPING',
    };
  }
  return {
    ...selfIdentity,
    result: 'UNRESOLVED',
    rawKind,
    rawIdentity: rawValue,
    canonicalPhone: null,
    resolutionSource: 'NO_PHONE_IDENTITY',
  };
}

function compareOwnerIdentity(expected, resolved) {
  const expectedPhone = canonicalPhoneIdentity(expected);
  if (!expectedPhone || !resolved || resolved.result !== 'RESOLVED') {
    return { result: 'UNRESOLVED', expectedPhone, actualPhone: resolved?.canonicalPhone || null };
  }
  return {
    result: expectedPhone === resolved.canonicalPhone ? 'MATCH' : 'MISMATCH',
    expectedPhone,
    actualPhone: resolved.canonicalPhone,
  };
}

module.exports = {
  authenticatedSelfAliases,
  canonicalPhoneIdentity,
  compareOwnerIdentity,
  resolveWhatsAppIdentity,
};
