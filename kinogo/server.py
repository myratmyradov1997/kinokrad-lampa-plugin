"""kinogo.film native Cinemar resolver; catalog, exact voice/episode and HLS proxy."""
import asyncio,base64,hashlib,json,os,re,time
from urllib.parse import urljoin,urlparse,quote
from collections import OrderedDict
from pathlib import Path
from aiohttp import web
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from playwright.async_api import async_playwright
SITE='https://kinogo.film'
PORT=int(os.getenv('PORT','5210'))
VERSION='0.2.1'

def allowed_page(u):return urlparse(u).scheme=='https' and urlparse(u).hostname=='kinogo.film'
def allowed_media(u):
 p=urlparse(u);return p.scheme=='https' and (p.hostname or '').endswith('.cinemap.cc')
def b64(u):return base64.urlsafe_b64encode(u.encode()).decode()
def unb64(u):return base64.urlsafe_b64decode(u).decode()
def leaves(nodes,path=()):
 for n in nodes:
  if n.get('folder'):yield from leaves(n['folder'],path+(n.get('title',''),))
  else:yield n,path

def parse_search(html):
 soup=BeautifulSoup(html,'html.parser');out=[]
 for el in soup.select('article.shortStory'):
  a=el.select_one('h2 a[href]');im=el.select_one('img')
  if not a or not allowed_page(a['href']):continue
  title=a.get_text(' ',strip=True);year=re.search(r'\((\d{4})\)',title)
  out.append({'id':int(re.search(r'/(\d+)-',a['href'])[1]),'title':re.sub(r'\s*\(\d{4}\)\s*$','',title),'year':year[1] if year else '', 'url':a['href'],'poster':urljoin(SITE,im.get('data-src') or im.get('src','')) if im else '', 'description':'','media_type':'movie'})
 return out

def playback(tree):
 opts=[];seasons={}
 for n,path in leaves(tree):
  fid=n['pjs_id'];label=n.get('title','');joined=' / '.join(path)
  sm=re.search(r'(?:сезон\s*(\d+)|(\d+)\s*сезон)',joined,re.I)
  em=re.search(r'(?:серия\s*(\d+)|(\d+)\s*серия)',joined+' / '+label,re.I)
  item={'file_id':fid,'label':BeautifulSoup(label,'html.parser').get_text(' ',strip=True),'quality':'Cinemar','path':list(path)}
  if sm and em:
   season=int(sm[1] or sm[2]);ep=int(em[1] or em[2]);voice=label if not re.search(r'серия',label,re.I) else next((v for v in path if not re.search(r'сезон|серия',v,re.I)),label)
   item.update(season=season,episode=ep,label=BeautifulSoup(voice,'html.parser').get_text(' ',strip=True))
   seasons.setdefault(season,{}).setdefault(ep,[]).append(item)
  else:
   item['label']=' / '.join(path+(label,));opts.append(item)
 if seasons:
  return {'type':'series','options':[], 'seasons':[{'season':s,'episodes':[{'episode':e,'translations':ts} for e,ts in sorted(es.items())]} for s,es in sorted(seasons.items())]}
 return {'type':'movie','options':opts,'seasons':[]}

class Source:
 def __init__(self):self.http=None;self.pw=None;self.browser=None;self.sessions=OrderedDict();self.lock=asyncio.Lock();self.browser_lock=asyncio.Lock();self.scripts={};self.warm_task=None;self.browser_error=None
 async def start(self):
  self.http=AsyncSession(impersonate='chrome',timeout=40)
  self.warm_task=asyncio.create_task(self.warm_browser())
 async def warm_browser(self):
  try:await self.ensure_browser()
  except Exception as exc:self.browser_error=str(exc)
 async def ensure_browser(self):
  async with self.browser_lock:
   if self.browser and self.browser.is_connected():return
   if self.pw is None:self.pw=await async_playwright().start()
   try:
    self.browser=await self.pw.chromium.launch(headless=True,timeout=15000,proxy={'server':os.getenv('KG_BROWSER_PROXY','http://127.0.0.1:7899')},args=['--disable-gpu','--disable-quic','--mute-audio'])
    self.browser_error=None
   except Exception as exc:
    self.browser=None
    self.browser_error='Не удалось запустить браузер подготовки Kinogo за 15 секунд'
    raise RuntimeError(self.browser_error) from exc
 async def close(self):
  if self.warm_task and not self.warm_task.done():
   self.warm_task.cancel()
   await asyncio.gather(self.warm_task,return_exceptions=True)
  if self.browser:await self.browser.close()
  if self.pw:await self.pw.stop()
  await self.http.close()
 async def search(self,q):
  r=await self.http.get(SITE+'/search/'+quote(q,safe=''));r.raise_for_status();return parse_search(r.text)
 async def get_media(self,url,headers=None):
  # The native player also rotates Cinemap mirrors. The recorded requests
  # demonstrate the same signed path on these hosts; retain that path exactly.
  parsed=urlparse(url);hosts=list(dict.fromkeys([parsed.hostname,'pegasus.host.cinemap.cc','rubidium.host.cinemap.cc','tornado.host.cinemap.cc']))
  last=None
  for host in hosts:
   candidate=parsed._replace(netloc=host).geturl()
   response=None
   try:
    response=await self.http.get(candidate,headers=headers or {'Referer':'https://cinemar.cc/'},proxy='http://127.0.0.1:7899',timeout=(5,25))
    response.raise_for_status()
    return response
   except Exception as exc:
    if response is not None and 400<=response.status_code<500:raise
    last=exc
  raise RuntimeError('Медиазеркала Cinemap временно недоступны') from last
 async def session(self,url):
  if not allowed_page(url):raise ValueError('invalid Kinogo page')
  async with self.lock:
   await self.ensure_browser()
   sess=self.sessions.get(url)
   if sess and time.monotonic()-sess['created']<1800 and not sess['page'].is_closed():return sess
   if sess:await sess['page'].close()
   r=await self.http.get(url);r.raise_for_status();soup=BeautifulSoup(r.text,'html.parser')
   iframe=soup.select_one('iframe[data-src*="cinemar.cc"]')
   if not iframe or urlparse(iframe['data-src']).hostname!='cinemar.cc':raise ValueError('На странице нет Cinemar')
   embed_html=await self.http.get(iframe['data-src'],headers={'Referer':url});embed_html.raise_for_status()
   page=await self.browser.new_page(viewport={'width':1920,'height':1080})
   async def doc(route):await route.fulfill(body=embed_html.text,content_type='text/html')
   await page.route(iframe['data-src'],doc)
   async def script(route):
    key=route.request.url
    body=self.scripts.get(key)
    if body is None:
     cached=Path(__file__).parent/'assets'/Path(urlparse(key).path).name
     if cached.is_file():body=cached.read_text()
     else:
      response=await self.http.get(key);response.raise_for_status();body=response.text
     self.scripts[key]=body
    body=body.replace('var api=function(t){var e=1,o=3e3;', 'var api=function(t){var e=1,o=15e3;')
    body=body.replace(',{preload:preload,init:init,play:play,reload:reload,current:current',',window.__nativePlaylist={preload:preload,init:init,play:play,reload:reload,current:current')
    body+=''';(()=>{const C=Cinemar;window.Cinemar=function(o){o.preroll=[];o.midroll=[];o.dp_vast=false;const P=Playerjs;window.Playerjs=function(p){window.__opts=p;window.__player=new P(p);return window.__player};return C(o)}})();'''
    await route.fulfill(body=body,content_type='application/javascript')
   await page.route('**/assets/*.js*',script)
   async def filt(route):
    if route.request.resource_type in ('image','font','media','stylesheet') or any(x in route.request.url for x in ('/vast','agl007','agl008','bidster','bracdn')):await route.abort()
    else:await route.fallback()
   await page.route('**/*',filt)
   try:
    await page.goto(iframe['data-src'],referer=url,wait_until='domcontentloaded',timeout=25000)
    await page.wait_for_function('window.__opts && Array.isArray(window.__opts.file)',timeout=30000)
    await page.wait_for_selector('.playlist-title',state='attached',timeout=15000)
    tree=await page.evaluate('window.__opts.file')
   except BaseException:await page.close();raise
   meta={}
   for sc in soup.select('script[type="application/ld+json"]'):
    try:
     obj=json.loads(sc.string or '{}')
     if obj.get('@type') in ('Movie','TVSeries'):meta=obj;break
    except (ValueError,AttributeError):pass
   title=meta.get('name') or soup.select_one('h1').get_text(' ',strip=True)
   sess={'page':page,'tree':tree,'lock':asyncio.Lock(),'created':time.monotonic(),'detail':{'title':title,'url':url,'embed_url':iframe['data-src'],'poster':meta.get('image',''),'playback':playback(tree)}}
   self.sessions[url]=sess
   while len(self.sessions)>4:
    _,old=self.sessions.popitem(last=False);await old['page'].close()
   return sess
 async def resolve(self,url,fid):
  sess=await self.session(url)
  async with sess['lock']:
   node,path=next(((n,p) for n,p in leaves(sess['tree']) if n['pjs_id']==fid),(None,None))
   if node is None:raise ValueError('Выбранной серии или озвучки нет')
   page=sess['page']
   # Use the site's native playlist API with the exact leaf id. It performs
   # the ordinary playlist/load request and preserves season/episode/voice.
   await page.evaluate("fid=>window.__nativePlaylist.play(fid)",fid)
   try:
    await page.wait_for_function("fid=>{const x=window.__nativePlaylist.current();return x&&x.pjs_id===fid&&x.file&&x.file!=='#'}",arg=fid,timeout=35000)
   except Exception as exc:
    raise ValueError('Cinemar не подготовил выбранную серию/озвучку за 35 секунд. Повтори запрос.') from exc
   chosen=await page.evaluate("window.__nativePlaylist.current()")
   media=chosen['file'];urls=re.findall(r'https?://[^\s,\[\]]+',media)
   if not urls or not allowed_media(urls[0]):raise ValueError('Недопустимый медиадомен')
   return urls[0],node['title']

@web.middleware
async def cors(request,handler):
 try:r=await handler(request) if request.method!='OPTIONS' else web.Response()
 except web.HTTPException as e:r=e
 except Exception as e:r=web.json_response({'error':str(e)},status=502)
 r.headers['Access-Control-Allow-Origin']='*';r.headers['Access-Control-Allow-Headers']='Range, Content-Type';return r

def make_app():
 app=web.Application(middlewares=[cors]);src=Source()
 async def startup(app):await src.start()
 async def cleanup(app):await src.close()
 app.on_startup.append(startup);app.on_cleanup.append(cleanup)
 def base(r):return str(r.url.origin())
 def proxy(r,u):return base(r)+'/media?u='+b64(u)
 async def health(r):return web.json_response({'ok':True,'service':'kinogo-film','version':VERSION,'site':SITE,'browser_ready':bool(src.browser and src.browser.is_connected()),'browser_error':src.browser_error})
 async def search(r):
  q=r.query.get('q',r.query.get('title',''))[:120];items=await src.search(q) if q else []
  return web.json_response({'query':q,'items':items,'count':len(items),'confident':False})
 async def detail(r):
  try:sess=await asyncio.wait_for(src.session(r.query.get('url','')),50)
  except asyncio.TimeoutError:raise RuntimeError('Подготовка списка серий Kinogo превысила 50 секунд. Повтори запрос.')
  return web.json_response(sess['detail'])
 async def resolve(r):
  u,label=await src.resolve(r.query.get('page_url',''),r.query.get('file_id',''))
  master=await src.get_media(u)
  qs={};lines=master.text.splitlines()
  for i,line in enumerate(lines):
   m=re.search(r'RESOLUTION=\d+x(\d+)',line)
   if m and i+1<len(lines):qs[int(m[1])]=proxy(r,urljoin(str(master.url),lines[i+1].strip()))
  if not qs:raise ValueError('Мастер не вернул качества')
  qualities={str(h)+'p':qs[h] for h in sorted(qs,reverse=True)}
  return web.json_response({'key':hashlib.sha256(u.encode()).hexdigest()[:20],'file_id':r.query.get('file_id'),'default_audio':0,'audios':[{'index':0,'label':label,'url':next(iter(qualities.values())),'quality':qualities,'qualities':sorted(qs,reverse=True)}],'tracks':[]})
 async def media(r):
  u=unb64(r.query.get('u',''))
  if not allowed_media(u):raise web.HTTPForbidden()
  headers={'Referer':'https://cinemar.cc/'}
  if r.headers.get('Range'):headers['Range']=r.headers['Range']
  up=await src.get_media(u,headers);body=up.content
  if body.startswith(b'#EXTM3U'):
   def wrap(u):return proxy(r,urljoin(str(up.url),u))
   out=[]
   for line in up.text.splitlines():
    if line.startswith('#'):out.append(re.sub(r'URI="([^"]+)"',lambda m:'URI="'+wrap(m[1])+'"',line))
    else:out.append(wrap(line.strip()) if line.strip() else '')
   return web.Response(text='\n'.join(out)+'\n',content_type='application/vnd.apple.mpegurl')
  rh={k:v for k,v in up.headers.items() if k.lower() in ('content-type','content-range','accept-ranges')}
  return web.Response(body=body,status=up.status_code,headers=rh)
 async def plugin(r):
  from pathlib import Path
  return web.Response(text=(Path(__file__).parent/'plugin.js').read_text().replace('__BASE_URL__',base(r)),content_type='application/javascript',headers={'Cache-Control':'no-store'})
 async def noop(r):return web.json_response({'ok':True})
 app.router.add_get('/api/health',health);app.router.add_get('/api/search',search);app.router.add_get('/api/prepare',search);app.router.add_get('/api/detail',detail);app.router.add_get('/api/resolve',resolve);app.router.add_get('/api/prefetch',noop);app.router.add_get('/media',media);app.router.add_get('/plugin.js',plugin)
 return app
if __name__=='__main__':web.run_app(make_app(),host='0.0.0.0',port=PORT,print=None)
