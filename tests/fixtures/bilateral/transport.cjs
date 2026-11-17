// Entirely synthetic provider adapter; actual classifier, bridge, spool and HMAC.
const path = require('node:path');
const {createInboundBridge} = require('../../../whatsapp-transport-local/src/bridge');
const {createOutboundProvenanceLedger} = require('../../../whatsapp-transport-local/src/outbound-provenance');
const {classifyFinalFromMeMessage} = require('../../../whatsapp-transport-local/src/transport');
const {resolveConversationIdentity} = require('../../../whatsapp-transport-local/src/conversation');
(async () => {
 const root=process.argv[2], tenant=process.argv[3];
 const owner='5500000000001@c.us', pn='5500000000025@c.us', lid='1000000000025@lid';
 const config={tenantId:tenant,sourceAccount:'synthetic',inboundSource:'wwebjs',inboundChannel:'whatsapp',
 inboundForwardEnabled:true,inboundForwardUrl:'http://localhost/events',inboundForwardHosts:['localhost'],
 hmacSecret:'synthetic-bilateral-hmac-32-bytes',inboundHttpTimeoutMs:1000,
 mediaRoot:path.join(root,'media'),mediaMaxBytes:100000,mediaDownloadTimeoutMs:1000,
 mediaNotificationUrl:'http://localhost/media',conversationCaptureOwnerVoiceEnabled:true,
 conversationCaptureOwnerTextEnabled:true,whatsappArtifactIngestionEnabled:true,
 conversationCaptureUnattributedOutboundMediaEnabled:true,outboundProvenanceDir:path.join(root,'ledger')};
 for(const [key,dir] of Object.entries({inboundSpoolDir:'spool',inboundPendingDir:'pending',inboundSendingDir:'sending',inboundSentDir:'sent',inboundQuarantineDir:'quarantine',mediaNotificationPendingDir:'mpending',mediaNotificationSendingDir:'msending',mediaNotificationSentDir:'msent',mediaNotificationQuarantineDir:'mquarantine'})) config[key]=path.join(root,dir);
 if(process.argv[4]==='canary') Object.assign(config, {semanticCanaryEnabled:true,
  semanticCanaryConversationKey:`wwebjs:${lid}`,semanticCanaryIngressUrl:'http://localhost/api/v1/ingress/internal/semantic-canary'});
 const packets=[]; let downloads=0;
 const bridge=createInboundBridge(config,{info(){},warn(){}},{fetchImpl:async(url,options)=>{
 packets.push({kind:url.endsWith('/media')?'media':'event',body:options.body.toString(),headers:options.headers});
 return {status:202,text:async()=>'{"status":"accepted","canary_media_allowed":true,"grants_authority":false}'};}});
 const ledger=createOutboundProvenanceLedger(config);
 let activeLid=lid, activePn=pn;
 const client={getContactLidAndPhone:async()=>[{lid:activeLid,pn:activePn}]};
 const identity={ownerVerified:true,pn:owner,wid:owner,aliases:[owner]};
 const natural=process.argv[4]==='natural';
 const stamp=natural?Date.parse('2026-11-10T12:00:00Z')/1000:Math.floor(Date.now()/1000)-60;
 const specs=[['rx-voice',false,'ptt','Trabalho na Oficina Aurora.'],['tx-voice',true,'ptt','Trabalho na Oficina Aurora.'],['rx-text',false,'chat','Minha irmã Clara chegou.'],['tx-text',true,'chat','Minha irmã Lia chegou.'],['automatic',true,'chat','Saída automática sintética.'],['missing',false,'ptt',null]];
 if(process.argv[4]==='canary') {
  specs[0][3]='Proponho viagem domingo.';
  specs[1][3]='Confirmo viagem domingo.';
  specs[2][3]='Podemos sair domingo às oito?';
  specs[3][3]='Confirmo domingo às oito.';
 }
 if(natural) {
  specs[0][3]='Proponho viagem para Serra Azul sábado.';
  specs[1][3]='Sábado não será possível; proponho domingo às 09:00.';
  specs[2][3]='Combinado domingo às 09:00.';
  specs[3][3]='Estou ciente.';
  specs.push(['joao',false,'chat','Estou ciente da viagem para Serra Azul domingo.']);
  specs.push(['pedro',false,'chat','Proponho passeio para Serra Azul domingo.']);
  specs.push(['tx-image',true,'image','Imagem inteiramente fictícia']);
  specs.push(['tx-document',true,'document','Documento inteiramente fictício']);
 }
 const stamps=specs.map((_,i)=>stamp+(natural?[0,172800,259200,259201,259202,259203,259204,259205,259206,259207][i]:i));
 for(let i=0;i<specs.length;i++) {
  const [id,fromMe,type,text]=specs[i];
  activeLid=id==='joao'?'1000000000026@lid':id==='pedro'?'1000000000027@lid':lid;
  activePn=id==='joao'?'5500000000026@c.us':id==='pedro'?'5500000000027@c.us':pn;
  const message={id:{_serialized:id,remote:activeLid,fromMe},fromMe,from:fromMe?owner:activeLid,to:fromMe?activeLid:owner,type,
   hasMedia:['ptt','image','document'].includes(type),body:type==='chat'?text:'',timestamp:stamps[i],
   _data:natural&&i===1?{quotedStanzaID:'rx-voice'}:natural&&i===2?{quotedStanzaID:'tx-voice'}:i===3?{quotedStanzaID:'rx-text'}:{},
   downloadMedia:async()=>{downloads++;return text===null?null:{mimetype:type==='image'?'image/png':type==='document'?'application/pdf':'audio/ogg',data:Buffer.from((type==='ptt'?'OggS ':'synthetic ')+text).toString('base64')};}};
  if(id==='automatic') ledger.begin({idempotency_key:'synthetic-auto',destination:lid,text});
  if(fromMe){ const r=await classifyFinalFromMeMessage(message,client,ledger,identity,{isCurrent:()=>true});
   message.__fromMeClassification=message.__finalFromMeClassification=r.classification;
   message.__fromMeClassificationReason=r.reason;message.__outboundProvenance=r.provenance;
  }else message.__conversationIdentity=await resolveConversationIdentity(message,client);
  const options={clientInfo:{wid:owner},authenticatedSelfIdentity:identity,authenticatedSelfAuthorityCurrent:true};
  await bridge.handleMessage(message,options);
  if(id==='tx-voice') await bridge.handleMessage(message,options); // replay must not download
 }
 process.stdout.write(JSON.stringify({packets,downloads,lid,pn,stamp,stamps,peers:natural?[pn,'5500000000026@c.us','5500000000027@c.us']:[pn],texts:specs.map(s=>s[3])}));
})().catch(()=>{process.stderr.write('SYNTHETIC_TRANSPORT_FAILED');process.exitCode=1;});
