const { resolveMessageLookupId } = require('./message-id');
async function recoverVoice(client, id, config) {
  const m = await require('./history-lookup').lookupVoice(client,id);
  if (!m) return {status:'unavailable',code:'VOICE_MESSAGE_NOT_FOUND'};
  const resolved = resolveMessageLookupId(m.id);
  if (resolved.lookupId !== id) return {status:'unavailable',code:'MEDIA_ID_MISMATCH'};
  if (!['ptt','audio'].includes(m.type)) return {status:'unavailable',code:'MEDIA_TYPE_MISMATCH'};
  if (!m.hasMedia) return {status:'unavailable',code:'VOICE_MEDIA_NOT_AVAILABLE'};
  const receiver = Object.create(m);
  receiver.id = {...m.id, _serialized:resolved.lookupId};
  // Native message filehash is evidence for a local file; never infer by contact/time.
  let media;
  const hash = m._data?.filehash;
  if (config.mediaRoot && typeof hash === 'string') {
    const fs = require('fs'), crypto = require('crypto'), path = require('path');
    const digest = Buffer.from(hash,'base64').toString('hex');
    if (/^[a-f0-9]{64}$/.test(digest)) {
      const filename = path.join(config.mediaRoot,digest.slice(0,2),digest);
      try {
        const st = fs.lstatSync(filename);
        if (st.isFile() && !st.isSymbolicLink() && st.size > 0 && st.size <= config.mediaMaxBytes) {
          const bytes = fs.readFileSync(filename);
          if (crypto.createHash('sha256').update(bytes).digest('hex') === digest)
            media = {data:bytes.toString('base64'),mimetype:m._data.mimetype};
        }
      } catch (e) { if (e.code !== 'ENOENT') throw e; }
    }
  }
  const mediaSource = media ? 'NATIVE_HASH_LOCAL' : 'WHATSAPP_WEB';
  if (!media) media = await m.downloadMedia.call(receiver);
  if (!media || !media.data) return {status:'unavailable',code:'VOICE_DOWNLOAD_EMPTY'};
  if (media.data.length > config.mediaMaxBytes*1.4+16) return {status:'unavailable',code:'MEDIA_TOO_LARGE'};
  return {status:'ok',message_id:resolved.lookupId,from_me:m.fromMe===true,
    timestamp:m.timestamp, source_account:config.sourceAccount,
    id_strategy:resolved.strategy,media_source:mediaSource,mimetype:media.mimetype.split(';')[0],data:media.data};
}
module.exports = {recoverVoice};
