#!/usr/bin/env node

const fs = require('node:fs');
const path = require('node:path');

const packagePath = path.join(__dirname, '..', 'whatsapp-transport-local', 'node_modules', 'whatsapp-web.js', 'package.json');
const packageInfo = JSON.parse(fs.readFileSync(packagePath, 'utf8'));
const { Client, Chat, Message } = require(path.dirname(packagePath));
const messageSource = fs.readFileSync(path.join(path.dirname(packagePath), 'src', 'structures', 'Message.js'), 'utf8');
const chatSource = fs.readFileSync(path.join(path.dirname(packagePath), 'src', 'structures', 'Chat.js'), 'utf8');

const methods = (prototype) => Object.getOwnPropertyNames(prototype).filter((name) => name !== 'constructor').sort();
const clientMethods = methods(Client.prototype);
const chatMethods = methods(Chat.prototype);
const messageMethods = methods(Message.prototype);

console.log(JSON.stringify({
  whatsapp_web_js_version: packageInfo.version,
  live_session_probe: 'NOT_RUN',
  reason: 'browser attach intentionally omitted while Andy Behavior canary is pending',
  capabilities: {
    can_list_chats: clientMethods.includes('getChats'),
    can_fetch_historical_messages: chatMethods.includes('fetchMessages'),
    can_fetch_group_authors: /this\.author\s*=/.test(messageSource),
    can_fetch_timestamps: /this\.timestamp\s*=/.test(messageSource),
    can_fetch_reply_references: messageMethods.includes('getQuotedMessage') ? 'PARTIAL' : 'NO',
    can_fetch_captions: /data\.caption/.test(messageSource) ? 'PARTIAL' : 'NO',
    can_paginate_history: 'LIMIT_ONLY',
    can_distinguish_from_me: /this\.fromMe\s*=/.test(messageSource),
    can_distinguish_direct_vs_group: /this\.isGroup\s*=/.test(chatSource),
    can_get_stable_source_message_id: /this\.id\s*=/.test(messageSource),
  },
  forbidden_methods_not_called: true,
}, null, 2));
