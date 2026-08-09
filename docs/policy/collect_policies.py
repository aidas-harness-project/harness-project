import json,re,hashlib,subprocess,datetime,csv
from pathlib import Path
from requests.utils import requote_uri
import requests
root=Path('docs/policy'); records=json.loads((root/'collection-manifest.json').read_text()); H={'User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36','Accept':'application/pdf,*/*'}; prior=json.loads((root/'sources.json').read_text()) if (root/'sources.json').exists() else []; results=list(prior); seen={x['sha256']:x['path'] for x in prior if x.get('sha256') and x.get('status')=='verified-pdf'}; today=datetime.date.today().isoformat(); already_today={x.get('source_url') for x in prior if x.get('collected_at')==today and x.get('status')=='verified-pdf'}
for i,r0 in enumerate(records,1):
 r=dict(r0); url=requote_uri(r['source_url']); slug=f'{i:02d}-{r["company_slug"]}-{r["insurance_type"]}'; out=root/r['company_slug']/r['insurance_type']/f'{slug}.pdf'; out.parent.mkdir(parents=True,exist_ok=True); n=1
 if url in already_today: continue
 while out.exists():
  n+=1; out=out.with_name(f'{slug}-{today}' + (f'-{n}' if n>2 else '') + '.pdf')
 r.update({'source_url':url,'collected_at':today,'path':str(out),'status':'pending'})
 try:
  with requests.get(url,headers=H,timeout=(20,180),stream=True,allow_redirects=True) as q:
   r['http_status']=q.status_code; r['content_type']=q.headers.get('content-type','')
   if q.status_code!=200: r['status']='http-error'; results.append(r); print(i,'FAIL',r['company_slug'],q.status_code,flush=True); continue
   with out.open('wb') as f:
    for chunk in q.iter_content(1024*1024):
     if chunk:f.write(chunk)
  b=out.read_bytes(); r['bytes']=len(b); r['sha256']=hashlib.sha256(b).hexdigest()
  if not b.startswith(b'%PDF-'): r['status']='not-pdf'; out.unlink(missing_ok=True); results.append(r); print(i,'FAIL not-pdf',r['company_slug'],flush=True); continue
  if r['sha256'] in seen: r['status']='duplicate'; r['duplicate_of']=seen[r['sha256']]; out.unlink(missing_ok=True); results.append(r); print(i,'DUP',r['company_slug'],flush=True); continue
  seen[r['sha256']]=str(out); info=subprocess.run(['pdfinfo',str(out)],capture_output=True,text=True,errors='replace',timeout=90); r['pdfinfo_ok']=info.returncode==0
  for line in info.stdout.splitlines():
   if line.startswith('Pages:'):
    try:r['pages']=int(line.split(':',1)[1].strip())
    except:pass
  text=subprocess.run(['pdftotext','-f','1','-l','2','-layout',str(out),'-'],capture_output=True,text=True,errors='replace',timeout=180).stdout; r['text_probe']=' '.join(text.split())[:500]; r['status']='verified-pdf' if r['pdfinfo_ok'] else 'pdf-signature-only'; print(i,'OK',r['company_slug'],r['insurance_type'],r['bytes'],r.get('pages','?'),flush=True)
 except Exception as e:
  r['status']='download-error'; r['error']=f'{type(e).__name__}: {e}';
  if out.exists() and out.stat().st_size==0:out.unlink()
  print(i,'ERROR',r['company_slug'],str(e)[:100],flush=True)
 results.append(r)
(root/'sources.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
fields=['company_slug','company','insurance_type','product','status','path','pages','bytes','sha256','http_status','content_type','collected_at','source_url','landing_page','error']
with (root/'index.csv').open('w',newline='',encoding='utf-8-sig') as f:
 w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows({k:x.get(k,'') for k in fields} for x in results)
print('RESULTS',len(results),'VERIFIED',sum(x['status'] in ('verified-pdf','pdf-signature-only') for x in results),'FAILED',sum(x['status'] not in ('verified-pdf','pdf-signature-only','duplicate') for x in results))
