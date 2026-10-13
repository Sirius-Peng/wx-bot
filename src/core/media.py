"""Bounded image fetch/decrypt/normalization; image bytes never enter chat logs."""
from __future__ import annotations
import asyncio,base64,io,ipaddress,socket,warnings
from urllib.parse import urlsplit,urljoin,urlencode
import httpx
from PIL import Image,ImageOps,UnidentifiedImageError
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from cryptography.hazmat.primitives.padding import PKCS7

MAX_DOWNLOAD=20*1024*1024
MAX_PIXELS=20_000_000
MAX_IMAGES=6
CDN='https://novac2c.cdn.weixin.qq.com/c2c'
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
        async with httpx.AsyncClient(timeout=15,follow_redirects=False,trust_env=False,transport=self.transport) as client:
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
