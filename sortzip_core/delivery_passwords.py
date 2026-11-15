"""Optional password payload for a task-wide delivery manifest."""
from hashlib import scrypt
import json
from secrets import token_bytes

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sortzip_core.i18n import tr


def _key(phrase,salt):
    return scrypt(phrase.encode('utf-8'),salt=salt,n=2**17,r=8,p=1,maxmem=160*1024**2,dklen=32)


def _aad(manifest):
    from sortzip_core.delivery_manifest import encode
    return b'SortZip task passwords v1\0'+encode({key:value for key,value in manifest.items() if key!='passwords'})


def protect(manifest,mode,passwords,phrase):
    if not valid_layers(passwords):
        raise ValueError(tr('交付密码记录无效。'))
    if mode=='plain':
        return {'mode':'plain','layers':passwords}
    if mode!='encrypted' or not 12<=len(phrase)<=1024:
        raise ValueError(tr('交付口令至少需要 12 个字符。'))
    salt,nonce = token_bytes(16),token_bytes(12)
    text = json.dumps(passwords,ensure_ascii=False).encode('utf-8')
    encrypted = AESGCM(_key(phrase,salt)).encrypt(nonce,text,_aad(manifest))
    return dict(mode='encrypted',salt=salt.hex(),nonce=nonce.hex(),data=encrypted.hex())


def valid_layers(data):
    return (isinstance(data,dict) and data.keys()<={str(index) for index in range(128)}
            and all(isinstance(value,str) and 0<len(value)<=1024 for value in data.values()))


def valid_envelope(value):
    if not isinstance(value,dict):
        return False
    if value.get('mode')=='plain':
        return value.keys()=={'mode','layers'} and valid_layers(value['layers'])
    if value.get('mode')!='encrypted' or value.keys()!={'mode','salt','nonce','data'}:
        return False
    def valid_hex(text,minimum,maximum):
        return (isinstance(text,str) and minimum<=len(text)<=maximum and len(text)%2==0
                and all(char in '0123456789abcdef' for char in text))
    return (valid_hex(value['salt'],32,32) and valid_hex(value['nonce'],24,24)
            and valid_hex(value['data'],32,1024*1024))


def unlock(manifest,phrase):
    value = manifest.get('passwords')
    if value is None:
        return {}
    if not valid_envelope(value):
        raise ValueError(tr('交付密码记录无效。'))
    if value['mode']=='plain':
        return value['layers']
    if not phrase or len(phrase)>1024:
        raise ValueError(tr('请输入交付口令。'))
    try:
        data = AESGCM(_key(phrase,bytes.fromhex(value['salt']))).decrypt(
            bytes.fromhex(value['nonce']),bytes.fromhex(value['data']),_aad(manifest))
        result = json.loads(data)
    except (InvalidTag,ValueError,RecursionError) as error:
        raise ValueError(tr('交付口令不正确，或清单已被修改。')) from error
    if not valid_layers(result):
        raise ValueError(tr('交付密码记录无效。'))
    return result
