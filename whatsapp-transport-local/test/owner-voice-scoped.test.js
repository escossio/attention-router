
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {createConfig}=require('../src/config');
const {createInboundBridge}=require('../src/bridge');
const {identityFromAliases}=require('../src/conversation');
const PEER='777700000@lid';
const OTHER='888800000@lid';
const SCOPE='wwebjs:'+PEER;
function fixture(t, extra={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'owner-voice-'));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const spool=path.join(root,'spool');
  return createConfig({
    LOCAL_TENANT_ID:'00000000-0000-4000-8000-000000000001',
    INBOUND_FORWARD_ENABLED:'true',
    CONVERSATION_CAPTURE_OWNER_VOICE_ENABLED:'true',
    CONVERSATION_CAPTURE_OWNER_VOICE_PEER_KEY:SCOPE,
    LOCAL_INBOUND_FORWARD_URL:'http://127.0.0.1:18102/api/v1/ingress/internal/events',
    LOCAL_MEDIA_NOTIFICATION_URL:'http://127.0.0.1:18102/internal/whatsapp/media',
    INTERNAL_INGRESS_HMAC_SECRET:'synthetic-secret-not-production-123456789',
    LOCAL_INBOUND_SPOOL_DIR:spool,
    LOCAL_INBOUND_PENDING_DIR:path.join(spool,'pending'),
    LOCAL_INBOUND_SENDING_DIR:path.join(spool,'sending'),
    LOCAL_INBOUND_SENT_DIR:path.join(spool,'sent'),
    LOCAL_INBOUND_QUARANTINE_DIR:path.join(spool,'quarantine'),
    WHATSAPP_MEDIA_ROOT:path.join(root,'media'),
    ...extra,
  });
}
function event({peer=PEER, origin='UNKNOWN_FROM_ME', media=true, type='ptt', group=false}={}){
 const id=group?'sample@g.us':peer;
 return {
  id:{_serialized:'true_'+id+'_SYNTHETIC_VOICE',remote:id,fromMe:true},
  from:'5500000000001@c.us',to:id,fromMe:true,type,body:'private text ignored',
  hasMedia:media,timestamp:1794950000,
  __fromMeClassification:origin,__finalFromMeClassification:origin,
  __fromMeClassificationReason:'UNMATCHED_MEDIA_FROM_ME',__ownerSelfChat:false,
  __conversationIdentity:identityFromAliases([id]),
  downloadMedia:async()=>({mimetype:'audio/ogg',data:Buffer.from('synthetic voice bytes').toString('base64')}),
 };
}
function respond(log){return async(u,r)=>{log.push({u,body:JSON.parse(r.body)});return {status:202,text:async()=>'{"status":"accepted"}'};};}
const logger={info(){},warn(){},error(){}};
test('selected fromMe voice delivers media receipt without making owner action',async t=>{
 const cfg=fixture(t),log=[];
 const res=await createInboundBridge(cfg,logger,{fetchImpl:respond(log)}).handleMessage(event());
 assert.equal(res.status,'delivered');
 assert.equal(res.media.status,'media_ready_notified');
 const ingress=log.find(x=>x.u===cfg.inboundForwardUrl)?.body;
 const media=log.find(x=>x.u===cfg.mediaNotificationUrl)?.body;
 assert.equal(ingress.event_origin,'UNKNOWN_FROM_ME');
 assert.equal(ingress.owner_authenticated,false);
 assert.equal(ingress.content,'');
 assert.equal(ingress.metadata.from_me,true);
 assert.equal(media.capture_status,'READY');
 assert.equal(media.external_event_id,ingress.external_event_id);
});
test('other conversations, groups and nonvoice never download',async t=>{
 const cfg=fixture(t);
 const variants=[
  event({peer:OTHER}),event({peer:OTHER,origin:'OWNER_MANUAL_OUTBOUND_OBSERVED'}),
  event({type:'chat'}),event({media:false}),event({group:true}),
 ];
 for(const m of variants){
  let downloads=0;
  m.downloadMedia=async()=>{downloads++;return {mimetype:'audio/ogg',data:'eA=='};};
  const res=await createInboundBridge(cfg,logger,{fetchImpl:respond([])}).handleMessage(m);
  assert.equal(res.media,null);assert.equal(downloads,0);
 }
});
test('off means off, malformed selection fails startup',t=>{
 const cfg=fixture(t,{CONVERSATION_CAPTURE_OWNER_VOICE_ENABLED:'false'});
 assert.equal(cfg.conversationCaptureOwnerVoiceEnabled,false);
 assert.throws(()=>fixture(t,{CONVERSATION_CAPTURE_OWNER_VOICE_PEER_KEY:'hi'}),/OWNER_VOICE_PEER_KEY_INVALID/);
});
test('global account voice includes other conversations and groups without authority',async t=>{
 const cfg=fixture(t,{CONVERSATION_CAPTURE_OWNER_VOICE_PEER_KEY:''});
 for(const m of [event({peer:OTHER}),event({group:true})]){
  const r=await createInboundBridge(cfg,logger,{fetchImpl:respond([])}).handleMessage(m);
  assert.equal(r.media.delivery.status,'accepted');
  assert.equal(r.normalized.owner_authenticated,false);
  assert.equal(r.normalized.event_origin,'UNKNOWN_FROM_ME');
 }
});
