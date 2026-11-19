// Exact identity search through the existing read-only historical pager.
async function lookupVoice(client, id) {
  const direct = await client.getMessageById(id);
  if (direct) return direct;
  const match = /^(true|false)_([^\s_]+@(?:lid|c\.us|g\.us|newsletter))_([A-Za-z0-9_-]+)$/.exec(id);
  if (!match) throw new Error('INVALID_ID');
  const model = await client.pupPage.evaluate(async ({id, remote}) => {
    const chat = await window.WWebJS.getChat(remote, {getAsModel:false});
    if (!chat) return null;
    const matches = m => (m.id?._serialized || m.id?.$1) === id;
    let messages = chat.msgs.getModelsArray();
    let pages = 0;
    while (!messages.some(matches) && messages.length < 1000 && pages < 20) {
      const older = await window.require('WAWebChatLoadMessages').loadEarlierMsgs({chat});
      pages++;
      if (!older?.length) break;
      messages = chat.msgs.getModelsArray();
    }
    const found = messages.filter(matches);
    if (found.length > 1) throw new Error('IDENTITY_AMBIGUOUS');
    if (!found.length && (messages.length >= 1000 || pages >= 20)) throw new Error('HISTORY_SCAN_LIMIT');
    return found.length ? window.WWebJS.getMessageModel(found[0]) : null;
  }, {id,remote:match[2]});
  if (!model) return null;
  const {Message} = require('whatsapp-web.js');
  return new Message(client, model);
}
module.exports = {lookupVoice};
