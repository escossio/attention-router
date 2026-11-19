const test=require('node:test');
const assert=require('node:assert/strict');
const {createServer}=require('../src/server');
const {signHistoryRequest}=require('../src/history');
test('historical voice requires HMAC and downloads without sending',async t=>{
 const secret='synthetic-history-secret-12345678901234567890';
 let downloads=0;
 const client={getMessageById:async id=>({id:{_serialized:id},type:'ptt',hasMedia:true,fromMe:true,
 downloadMedia:async()=>{downloads++;return {mimetype:'audio/ogg',data:'T2dnUw=='};}}),
 sendMessage:()=>{throw new Error('FORBIDDEN_SEND');}};
 const cfg={historyReadOnlyEnabled:true,historyHmacSecret:secret,mediaMaxBytes:1000};
 const status={service_state:'ready',browser_debug_reachable:true,wwebjs_connected:true,ready:true,client_state:'CONNECTED'};
 const server=createServer(cfg,status,client,{outboundProvenance:{}});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));t.after(()=>server.close());
 const path='/internal/history/voice-media/synthetic';
 const url='http://127.0.0.1:'+server.address().port+path;
 assert.equal((await fetch(url)).status,503);assert.equal(downloads,0);
 const r=await fetch(url,{headers:signHistoryRequest(path,secret)});
 assert.equal(r.status,200);assert.equal((await r.json()).message_id,'synthetic');assert.equal(downloads,1);
});
