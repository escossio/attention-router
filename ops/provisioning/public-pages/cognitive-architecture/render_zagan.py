#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess, tempfile, urllib.request
from pathlib import Path

MAX_CHARS=900

def sentences(text:str):
    import re
    return [x.strip() for x in re.split(r'(?<=[.!?])\s+', text.strip()) if x.strip()]

def chunk(text:str):
    out=[]; cur=''
    for sent in sentences(text):
        if len(sent)>MAX_CHARS:
            words=sent.split()
            part=''
            for w in words:
                test=(part+' '+w).strip()
                if len(test)>MAX_CHARS and part:
                    out.append(part); part=w
                else: part=test
            if part:
                if cur: out.append(cur); cur=''
                out.append(part)
            continue
        test=(cur+' '+sent).strip()
        if len(test)>MAX_CHARS and cur:
            out.append(cur); cur=sent
        else: cur=test
    if cur: out.append(cur)
    return out

def probe(path:Path)->float:
    cmd=['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)]
    return float(subprocess.check_output(cmd,text=True).strip())

def post_json(url:str,payload:dict)->dict:
    data=json.dumps(payload,ensure_ascii=False).encode()
    req=urllib.request.Request(url,data=data,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=60) as r:
        return json.loads(r.read().decode())

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--source',required=True)
    ap.add_argument('--output-dir',required=True)
    ap.add_argument('--tts-container',default='tts-api-switcher')
    ap.add_argument('--generated-dir',default='/srv/projetos/tts-api-switcher/app/generated')
    ap.add_argument('--force',action='store_true')
    args=ap.parse_args()
    source=Path(args.source); outdir=Path(args.output_dir); outdir.mkdir(parents=True,exist_ok=True)
    audio_dir=outdir/'audio'; audio_dir.mkdir(exist_ok=True)
    raw=source.read_bytes(); source_hash=hashlib.sha256(raw).hexdigest()
    manifest_path=audio_dir/'manifest.json'; final=audio_dir/'zagan-cognitive-architecture.mp3'; segments_path=outdir/'segments.json'
    if not args.force and manifest_path.exists() and final.exists() and segments_path.exists():
        try:
            if json.loads(manifest_path.read_text())['source_sha256']==source_hash:
                print('audio already current'); return
        except Exception: pass

    content=json.loads(raw)
    paragraphs=[]
    for s in content['sections']:
        paragraphs.extend(s['paragraphs'])

    ip=subprocess.check_output(['docker','inspect','-f','{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}',args.tts_container],text=True).strip()
    if not ip: raise SystemExit('tts container has no address')
    url=f'http://{ip}:8090/api/generate-audio'
    generated=Path(args.generated_dir)

    with tempfile.TemporaryDirectory(prefix='andy-cognitive-zagan-') as td:
        td=Path(td); files=[]; segs=[]; cursor=0.0; seg_index=0
        for pidx,paragraph in enumerate(paragraphs):
            parts=chunk(paragraph)
            part_files=[]; part_duration=0.0
            for part in parts:
                payload={'text':part,'provider':'xai','language':'pt-BR','voice':'zagan','speed':1.0,'humanization':{'enabled':False}}
                data=post_json(url,payload)
                if data.get('status')!='ok': raise RuntimeError(data)
                src=generated/data['filename']; dst=td/f'{seg_index:04d}.mp3'; shutil.copy2(src,dst)
                dur=probe(dst); files.append(dst); part_files.append(dst); part_duration+=dur; seg_index+=1
            segs.append({'start':round(cursor,6),'duration':round(part_duration,6),'text':paragraph})
            cursor+=part_duration

        concat=td/'concat.txt'
        concat.write_text(''.join("file '"+str(p).replace("'","'\\''")+"'\n" for p in files))
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(concat),'-c','copy',str(final)],check=True)
        segments_path.write_text(json.dumps(segs,ensure_ascii=False,indent=2)+'\n')
        manifest={'source_sha256':source_hash,'voice':'zagan','provider':'xai','segments':len(segs),'chunks':len(files),'duration_seconds':round(cursor,3),'audio_sha256':hashlib.sha256(final.read_bytes()).hexdigest()}
        manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(manifest,ensure_ascii=False))
if __name__=='__main__': main()
