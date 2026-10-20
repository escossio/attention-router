// whatsapp-web.js 1.34.7 copies a media model's __x_id into the outgoing
// message. WhatsApp Web then rejects the message before sendMessage resolves.
const fs = require('node:fs');
const path = require('node:path');

const packageRoot = path.dirname(require.resolve('whatsapp-web.js/package.json'));
const packageInfo = require(path.join(packageRoot, 'package.json'));
if (packageInfo.version !== '1.34.7') {
  throw new Error('WWEBJS_MEDIA_PATCH_VERSION_MISMATCH');
}

const target = path.join(packageRoot, 'src/util/Injected/Utils.js');
const before = fs.readFileSync(target, 'utf8');
const anchor = "            ...extraOptions,\n        };\n\n        // Bot's won't reply";
if (before.split(anchor).length !== 2 || before.includes('delete message.__x_id;')) {
  throw new Error('WWEBJS_MEDIA_PATCH_ANCHOR_MISMATCH');
}
const after = before.replace(
  anchor,
  "            ...extraOptions,\n        };\n        delete message.__x_id;\n\n        // Bot's won't reply",
);
fs.writeFileSync(target, after);
console.log('WWEBJS_MEDIA_PATCH_APPLIED');
