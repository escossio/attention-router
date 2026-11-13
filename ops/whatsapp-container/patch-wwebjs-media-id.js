// whatsapp-web.js 1.34.8 contains both compatibility fixes required by the
// transport: inbound media is resolved through WhatsApp's own media cache and
// outbound media drops the internal __x_id before message initialization.
// Fail the image build closed if the pinned dependency or either contract drifts.
const fs = require('node:fs');
const path = require('node:path');

const packageRoot = path.dirname(require.resolve('whatsapp-web.js/package.json'));
const packageInfo = require(path.join(packageRoot, 'package.json'));
if (packageInfo.version !== '1.34.8') {
  throw new Error('WWEBJS_MEDIA_CONTRACT_VERSION_MISMATCH');
}

const target = path.join(packageRoot, 'src/util/Injected/Utils.js');
const source = fs.readFileSync(target, 'utf8');
const requiredAnchors = [
  'window.WWebJS.resolveMediaBlob = async (msgId) => {',
  'await msg.downloadMedia({',
  'InMemoryMediaBlobCache.get(',
  'delete message.__x_id;',
];
for (const anchor of requiredAnchors) {
  if (source.split(anchor).length !== 2) {
    throw new Error('WWEBJS_MEDIA_CONTRACT_ANCHOR_MISMATCH');
  }
}
console.log('WWEBJS_MEDIA_CONTRACT_VERIFIED');
