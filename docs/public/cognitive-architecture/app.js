const state={content:null,segments:[],blocks:[],active:-1,raf:0};
const player=document.querySelector('#player');
const statusEl=document.querySelector('#audio-status');
const gate=document.querySelector('#experience-gate');
const startExperience=document.querySelector('#start-experience');
const skipAudio=document.querySelector('#skip-audio');
document.body.classList.add('gate-open');

function closeGate(){
  gate.classList.add('hidden');
  document.body.classList.remove('gate-open');
  window.setTimeout(()=>gate.remove(),300);
}

function playNarration(){
  player.playbackRate=+document.querySelector('#rate').value;
  return player.play().then(()=>{
    statusEl.textContent='Zargan · reproduzindo…';
    cancelAnimationFrame(state.raf);
    animate();
  });
}

startExperience.onclick=()=>{
  playNarration().then(closeGate).catch(()=>{
    statusEl.textContent='O navegador ainda não liberou o áudio. Toque em Reproduzir.';
    closeGate();
  });
};
skipAudio.onclick=closeGate;

function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

async function loadContent(){
  const r=await fetch('content.json',{cache:'no-store'}); if(!r.ok)throw new Error('content');
  const c=await r.json(); state.content=c;
  document.querySelector('#thesis').textContent=c.thesis;
  document.querySelector('#passage-primary').textContent=c.passage[0];
  document.querySelector('#passage-secondary').textContent=c.passage[1];
  const article=document.querySelector('#article');
  c.sections.forEach((s,si)=>{
    const sec=document.createElement('section'); sec.className='doc-section';
    const h=document.createElement('h3'); h.textContent=s.title; h.className='narration-segment'; sec.appendChild(h);
    s.paragraphs.forEach((p,pi)=>{
      const el=document.createElement('p'); el.textContent=p;
      el.dataset.segmentKey=si+':'+pi; el.className='narration-segment';
      sec.appendChild(el);
    });
    article.appendChild(sec);
  });
  const refs=document.querySelector('#references');
  c.references.forEach(ref=>{
    const a=document.createElement('a'); a.href=ref.url; a.target='_blank'; a.rel='noopener noreferrer';
    a.innerHTML='<strong>'+escapeHtml(ref.name)+'</strong><span>'+escapeHtml(ref.url)+'</span>'; refs.appendChild(a);
  });
}

function decorateWords(){
  state.blocks=[];
  document.querySelectorAll('.narration-segment').forEach(p=>{
    const raw=p.textContent.trim(); p.textContent='';
    const words=[],weights=[];
    raw.split(/(\s+)/).forEach(x=>{
      if(/^\s+$/.test(x)){p.appendChild(document.createTextNode(x));return}
      if(!x)return; const w=document.createElement('span'); w.className='word'; w.textContent=x; p.appendChild(w);
      words.push(w); weights.push(Math.max(1,x.replace(/[^A-Za-zÀ-ÿ0-9]/g,'').length));
    });
    state.blocks.push({el:p,words,weights,total:weights.reduce((a,b)=>a+b,0)});
  });
}

async function loadSegments(){
  try{
    const r=await fetch('segments.json',{cache:'no-store'}); if(!r.ok)throw new Error();
    state.segments=await r.json();
  }catch{state.segments=[]}
}

function clearActive(){
  document.querySelectorAll('.narration-segment.active,.word.active').forEach(e=>e.classList.remove('active'));
  state.active=-1;
}
function sync(){
  if(!state.segments.length||!state.blocks.length)return;
  const t=player.currentTime;
  let idx=state.segments.findIndex(x=>t>=x.start&&t<x.start+x.duration);
  if(idx<0)idx=Math.max(0,state.segments.length-1);
  if(idx>=state.blocks.length)return;
  if(idx!==state.active){clearActive();state.active=idx;state.blocks[idx].el.classList.add('active');state.blocks[idx].el.scrollIntoView({behavior:'smooth',block:'center'})}
  const seg=state.segments[idx],b=state.blocks[idx]; b.words.forEach(w=>w.classList.remove('active'));
  const progress=Math.max(0,Math.min(1,(t-seg.start)/Math.max(.01,seg.duration)));
  const target=progress*b.total; let sum=0,wi=Math.max(0,b.words.length-1);
  for(let i=0;i<b.weights.length;i++){sum+=b.weights[i];if(target<=sum){wi=i;break}}
  if(b.words[wi])b.words[wi].classList.add('active');
}
function animate(){sync();if(!player.paused&&!player.ended)state.raf=requestAnimationFrame(animate)}

document.querySelector('#play').onclick=()=>{playNarration().catch(()=>statusEl.textContent='Toque novamente para liberar o áudio.')};
document.querySelector('#pause').onclick=()=>{player.pause();statusEl.textContent='Pausado.'};
document.querySelector('#stop').onclick=()=>{player.pause();player.currentTime=0;clearActive();statusEl.textContent='Interrompido.'};
document.querySelector('#rate').onchange=e=>{player.playbackRate=+e.target.value};
player.addEventListener('loadedmetadata',()=>statusEl.textContent='Zargan pronta · '+Math.round(player.duration/60)+' min de narração.');
player.addEventListener('ended',()=>{cancelAnimationFrame(state.raf);clearActive();statusEl.textContent='Leitura concluída.'});
player.addEventListener('error',()=>statusEl.textContent='Narração ainda não publicada ou indisponível.');

player.load();\nPromise.all([loadContent(),loadSegments()]).then(()=>decorateWords()).catch(()=>statusEl.textContent='Falha ao carregar o conteúdo.');
