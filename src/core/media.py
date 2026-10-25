"""Bounded image fetch/decrypt/normalization; image bytes never enter chat logs."""
from __future__ import annotations
import asyncio,base64,io,ipaddress,socket,warnings,logging,json,hashlib,time
from collections import OrderedDict
from urllib.parse import urlsplit,urljoin,urlencode
import httpx
from PIL import Image,ImageOps,UnidentifiedImageError
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from cryptography.hazmat.primitives.padding import PKCS7

MAX_DOWNLOAD=20*1024*1024
MAX_PIXELS=20_000_000
MAX_IMAGES=6
CDN='https://novac2c.cdn.weixin.qq.com/c2c'
log=logging.getLogger(__name__)
class MediaError(ValueError):pass

def aes_key(ref):
    try:
        if ref.get('key_hex'):key=bytes.fromhex(ref['key_hex'])
        elif ref.get('aes_key'):
            key=base64.b64decode(ref['aes_key'],validate=True)
            if len(key)==32:key=bytes.fromhex(key.decode('ascii'))
        else:return None
        if len(key)!=16:raise ValueError()
        return key
    except (ValueError,UnicodeError) as exc:raise MediaError('Invalid image key') from exc

def decrypt_image(data,ref):
    key=aes_key(ref)
    if key is None:return data
    try:
        cipher=Cipher(algorithms.AES(key),modes.ECB()).decryptor()
        padded=cipher.update(data)+cipher.finalize();unpad=PKCS7(128).unpadder()
        return unpad.update(padded)+unpad.finalize()
    except ValueError as exc:raise MediaError('Image decryption failed') from exc

def image_data_url(data):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                w,h=original.size
                if w*h>MAX_PIXELS or w<=0 or h<=0:raise MediaError('Image dimensions exceed limit')
                image=ImageOps.exif_transpose(original);image.thumbnail((2048,2048))
                if image.mode in ('RGBA','LA') or 'transparency' in image.info:
                    rgba=image.convert('RGBA');background=Image.new('RGBA',rgba.size,'white')
                    image=Image.alpha_composite(background,rgba).convert('RGB')
                else:image=image.convert('RGB')
                out=io.BytesIO();mime='image/jpeg'
                # Preserve small screenshots/diagrams losslessly for legible text.
                if original.format=='PNG':
                    image.save(out,format='PNG');mime='image/png'
                if not out.tell() or out.tell()>4*1024*1024:
                    out=io.BytesIO();image.save(out,format='JPEG',quality=90);mime='image/jpeg'
        if len(out.getvalue())>4*1024*1024:raise MediaError('Normalized image too large')
        return 'data:'+mime+';base64,'+base64.b64encode(out.getvalue()).decode('ascii')
    except (UnidentifiedImageError,OSError,Image.DecompressionBombError,Image.DecompressionBombWarning) as exc:
        raise MediaError('Invalid image bytes') from exc

class ImageLoader:
    def __init__(self,allowed_origins=(),transport=None):
        self.allowed_origins={self.origin(u) for u in allowed_origins if u}
        self.transport=transport
        self._client=None
        self._downloads=asyncio.Semaphore(3)
        self._decodes=asyncio.Semaphore(2)
        self._cache=OrderedDict(); self._cache_bytes=0; self._jobs={}

    async def aclose(self):
        for task in self._jobs.values(): task.cancel()
        await asyncio.gather(*self._jobs.values(),return_exceptions=True)
        self._jobs.clear();self._cache.clear();self._cache_bytes=0
        if self._client:await self._client.aclose();self._client=None

    @staticmethod
    def _key(ref):
        return hashlib.sha256(json.dumps(ref,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def prefetch_message(self,msg,adapter):
        resolver=None
        if hasattr(adapter,'resolve_images'):
            async def resolver(ref):
                values=await adapter.resolve_images([ref])
                if not values: raise MediaError('Image reference missing')
                return values[0]
        for ref in msg.images[:MAX_IMAGES]:self._start(ref,resolver)

    def _start(self,ref,resolver=None):
        key=self._key(ref);now=time.monotonic()
        for old,(at,value,size) in list(self._cache.items()):
            if now-at>180:self._cache.pop(old);self._cache_bytes-=size
        if key in self._cache:
            self._cache.move_to_end(key);return None
        if key in self._jobs:return self._jobs[key]
        if len(self._jobs)>=24:return None
        async def work():
            async with self._downloads:
                resolved=await resolver(ref) if resolver else ref
                data=await self.fetch(resolved)
            async with self._decodes:url=await asyncio.to_thread(image_data_url,data)
            result={'type':'image_url','image_url':{'url':url,'detail':'auto'}}
            size=len(url)
            self._cache[key]=(time.monotonic(),result,size);self._cache_bytes+=size
            while self._cache_bytes>64*1024*1024:
                _,(_,_,removed)=self._cache.popitem(last=False);self._cache_bytes-=removed
            return result
        task=asyncio.create_task(asyncio.wait_for(work(),35));self._jobs[key]=task
        def finished(done):
            self._jobs.pop(key,None)
            if not done.cancelled():done.exception()  # consume errors even for prefetch-only jobs
        task.add_done_callback(finished)
        return task

    async def _cached(self,ref,resolver=None):
        task=self._start(ref,resolver);key=self._key(ref)
        if key in self._cache:return self._cache[key][1]
        if task is None:raise MediaError('Image preparation queue full')
        return await asyncio.shield(task)
    @staticmethod
    def origin(url):
        u=urlsplit(url);return (u.scheme,u.hostname,u.port or (443 if u.scheme=='https' else 80))
    async def validate_url(self,url):
        u=urlsplit(url)
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password:raise MediaError('Invalid image address')
        if self.origin(url) in self.allowed_origins:return
        try:
            addresses=await asyncio.to_thread(socket.getaddrinfo,u.hostname,u.port or 443,type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise MediaError('Image address is not a public endpoint')
        except OSError as exc:raise MediaError('Image address lookup failed') from exc
    async def fetch(self,ref):
        inline=ref.get('inline')
        if inline:
            if len(inline)>MAX_DOWNLOAD*4//3+8:raise MediaError('Image exceeds byte limit')
            try:return base64.b64decode(inline,validate=True)
            except ValueError as exc:raise MediaError('Invalid inline image') from exc
        url=ref.get('url') or (CDN+'/download?'+urlencode({'encrypted_query_param':ref['encrypt_query_param']}) if ref.get('encrypt_query_param') else '')
        if self._client is None:
            self._client=httpx.AsyncClient(timeout=15,follow_redirects=False,trust_env=False,transport=self.transport)
        client=self._client
        for _ in range(4):
                await self.validate_url(url)
                async with client.stream('GET',url) as r:
                    if r.status_code in (301,302,303,307,308):
                        url=urljoin(url,r.headers.get('location',''));continue
                    r.raise_for_status()
                    if int(r.headers.get('content-length',0))>MAX_DOWNLOAD:raise MediaError('Image exceeds byte limit')
                    data=bytearray()
                    async for chunk in r.aiter_bytes():
                        if len(data)+len(chunk)>MAX_DOWNLOAD:raise MediaError('Image exceeds byte limit')
                        data.extend(chunk)
                    return decrypt_image(bytes(data),ref)
        raise MediaError('Image redirect limit exceeded')
    async def load(self,images):
        if len(images)>MAX_IMAGES:raise MediaError('Too many images in one turn')
        try:
            # Bounded parallelism; each input retains its original order.
            semaphore=asyncio.Semaphore(2)
            async def one(ref):
                async with semaphore:
                    data=await self.fetch(ref);url=await asyncio.to_thread(image_data_url,data)
                    return {'type':'image_url','image_url':{'url':url,'detail':'auto'}}
            tasks=[asyncio.create_task(one(ref)) for ref in images]
            try:return await asyncio.wait_for(asyncio.gather(*tasks),timeout=35)
            finally:
                for task in tasks:
                    if not task.done():task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
        except (httpx.HTTPError,ValueError,TimeoutError) as exc:raise MediaError('Image could not be loaded') from exc

    async def load_partial(self,images,resolver=None):
        """Keep original indices; one corrupt/late image cannot erase its siblings."""
        existing=set(self._jobs)
        async def one(ref):
            try:return await self._cached(ref,resolver)
            except Exception as error:
                log.warning('Media input skipped error_type=%s status=%s',type(error).__name__,getattr(getattr(error,'response',None),'status_code',None))
                return None
        tasks=[asyncio.create_task(one(ref)) for ref in images[:MAX_IMAGES]]
        try:
            await asyncio.wait(tasks,timeout=35) if tasks else None
            return [(task.result() if task.done() and not task.cancelled() else None) for task in tasks]+[None]*max(0,len(images)-MAX_IMAGES)
        finally:
            for task in tasks:
                if not task.done():task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            owned=[job for key,job in self._jobs.items() if key not in existing and not job.done()]
            for job in owned:job.cancel()
            await asyncio.gather(*owned,return_exceptions=True)
