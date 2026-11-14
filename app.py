import os
import re
import secrets
import hashlib
import hmac
import base64
import io
import zipfile
import shutil
import json
import time
import threading
import uuid
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, jsonify, request, send_file, redirect, session, url_for, after_this_request
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
# 上传上限 50GB，和 Caddy 的 request_body max_size 对齐
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)

# --- 密钥：绝不写死默认值 ---
# 未配 SECRET_KEY 时每次启动生成一个临时的，重启后旧 session 失效，
# 并在日志里大声警告；生产部署必须在 .env / 环境变量里配一个固定的。
_SECRET_KEY = os.getenv('SECRET_KEY')
if not _SECRET_KEY:
    _SECRET_KEY = secrets.token_hex(32)
    app.logger.warning('SECRET_KEY 未配置，已生成临时密钥（重启后登录态失效）。请在 .env 里设置一个固定值。')
app.secret_key = _SECRET_KEY

# --- 基础配置 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.getenv('STORAGE_PATH', os.path.join(BASE_DIR, 'storage')))
TRASH_DIR = os.path.abspath(os.getenv('TRASH_PATH', os.path.join(BASE_DIR, 'trash')))
SHARE_DIR = os.path.abspath(os.getenv('SHARE_PATH', os.path.join(BASE_DIR, 'shares')))
TRASH_META_FILE = os.path.join(TRASH_DIR, 'metadata.json')
SHARE_META_FILE = os.path.join(SHARE_DIR, 'metadata.json')

ADMIN_USER = os.getenv('ADMIN_USER', 'admin')
ADMIN_PASS = os.getenv('ADMIN_PASS', 'admin123')
if ADMIN_PASS == 'admin123':
    app.logger.warning('正在使用默认管理员密码 admin123，请立即在 .env 里改掉（install.sh 会自动生成随机密码）。')

# 分享默认有效期（天），0 = 永不过期；可配 SHARE_EXPIRE_DAYS
try:
    SHARE_EXPIRE_DAYS = int(os.getenv('SHARE_EXPIRE_DAYS', '30'))
except ValueError:
    SHARE_EXPIRE_DAYS = 30

for d in [ROOT_DIR, TRASH_DIR, SHARE_DIR]:
    if not os.path.exists(d):
        os.makedirs(d)

# --- 缩略图 + 照片时间线 ---
THUMB_DIR = os.path.join(SHARE_DIR, 'thumbs')
THUMB_SIZE = (320, 320)
THUMB_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
PHOTO_INDEX_FILE = os.path.join(SHARE_DIR, 'photo_index.json')
_PHOTO_INDEX = {}
_PHOTO_INDEX_LOADED = False
_PHOTO_LOCK = threading.Lock()


def _thumb_key(rel_path, mtime, size):
    return hashlib.sha1(f'{rel_path}|{mtime}|{size}'.encode()).hexdigest()


def _prune_thumbs(max_files=3000, keep=2500):
    """缩略图缓存超过上限时删最旧的，防止无限增长。"""
    try:
        names = [os.path.join(THUMB_DIR, n) for n in os.listdir(THUMB_DIR)]
        files = sorted((f for f in names if os.path.isfile(f)), key=os.path.getmtime)
        if len(files) > max_files:
            for f in files[:len(files) - keep]:
                try:
                    os.remove(f)
                except OSError:
                    pass
    except OSError:
        pass


def _photo_date(abs_path):
    """读 EXIF 拍摄日期（DateTimeOriginal 优先），没有则返回 None（调用方用文件 mtime）。"""
    try:
        with Image.open(abs_path) as im:
            exif = im.getexif()
            raw = exif.get(36867) or exif.get(36868) or exif.get(306)
            if raw:
                date_part = str(raw).split(' ')[0].replace(':', '-')
                if re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_part):
                    return date_part
    except Exception:
        pass
    return None


def _load_photo_index():
    global _PHOTO_INDEX, _PHOTO_INDEX_LOADED
    if _PHOTO_INDEX_LOADED:
        return
    _PHOTO_INDEX_LOADED = True
    try:
        if os.path.isfile(PHOTO_INDEX_FILE):
            with open(PHOTO_INDEX_FILE, 'r', encoding='utf-8') as f:
                _PHOTO_INDEX = json.load(f) or {}
    except Exception:
        _PHOTO_INDEX = {}


def _save_photo_index():
    tmp = PHOTO_INDEX_FILE + f'.tmp.{os.getpid()}'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_PHOTO_INDEX, f)
        os.replace(tmp, PHOTO_INDEX_FILE)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass

# --- 分片上传：断点续传临时目录（在 storage 之外，不会出现在文件列表里）---
UPLOAD_TMP_DIR = os.path.join(BASE_DIR, '.upload_tmp')
os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)
CHUNK_TTL_SECONDS = 24 * 3600  # 超过 24 小时没动静的分片视为废弃，下次 init 时清理
MAX_CHUNKS = 20000  # 50GB / 5MB 分片约 10240 片，留足余量

CATEGORY_EXTENSIONS = {
    'image': ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg', '.tiff', '.ico'],
    'video': ['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.3gp'],
    'doc':   ['.txt', '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.md', '.csv', '.json'],
    'app':   ['.exe', '.dmg', '.pkg', '.apk', '.ipa', '.deb', '.rpm', '.msi']
}

# ================= 安全路径：所有用户传来的路径都走这里 =================
# 规则：拒绝绝对路径，解析符号链接后必须仍在 base 目录内，否则返回 None。
# 这是修复任意文件读取（/api/file、/api/download_result 等）的核心。

def safe_join(base, *paths):
    base_resolved = os.path.realpath(base)
    for p in paths:
        if p is None:
            return None
        p = str(p).replace('\\', '/')
        if not p or p.startswith('/') or (len(p) > 1 and p[1] == ':'):
            return None
    joined = os.path.realpath(os.path.join(base_resolved, *paths))
    if joined != base_resolved and not joined.startswith(base_resolved + os.sep):
        return None
    return joined


def safe_name(name):
    """单个文件名/目录名：去掉路径分隔符，拒绝空、.、..。"""
    if not name:
        return None
    name = secure_filename(str(name))
    if not name or name in ('.', '..'):
        return None
    return name


def get_json():
    return request.json if isinstance(request.json, dict) else {}


def _parse_opt_int(v):
    """可选整数：None/'' → None；非法 → None；否则 int。"""
    if v in (None, ''):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _upload_dir(upload_id):
    if not re.fullmatch(r'[0-9a-f]{32}', upload_id or ''):
        return None
    return os.path.join(UPLOAD_TMP_DIR, upload_id)


def _upload_meta(upload_id):
    d = _upload_dir(upload_id)
    if not d:
        return None
    p = os.path.join(d, 'meta.json')
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _uploaded_chunks(upload_id, total):
    d = _upload_dir(upload_id)
    if not d:
        return []
    return [i for i in range(total)
            if os.path.isfile(os.path.join(d, f'{i}.part'))]


def _cleanup_stale_uploads():
    now = time.time()
    try:
        for name in os.listdir(UPLOAD_TMP_DIR):
            p = os.path.join(UPLOAD_TMP_DIR, name)
            if os.path.isdir(p) and now - os.path.getmtime(p) > CHUNK_TTL_SECONDS:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def _is_expired(info):
    exp = info.get('expires_at')
    if not exp:
        return False
    try:
        return datetime.now() > datetime.fromisoformat(exp)
    except ValueError:
        return False


# --- 登录限流：同一 IP 5 次失败锁 5 分钟 ---
_LOGIN_ATTEMPTS = {}
_LOGIN_LOCK = threading.Lock()
MAX_LOGIN_FAILS = 5
LOGIN_LOCK_SECONDS = 300


def _login_locked(ip):
    with _LOGIN_LOCK:
        rec = _LOGIN_ATTEMPTS.get(ip)
        if not rec:
            return False
        if rec.get('locked_until', 0) > time.monotonic():
            return True
        if time.monotonic() - rec.get('first', 0) > LOGIN_LOCK_SECONDS:
            _LOGIN_ATTEMPTS.pop(ip, None)
            return False
        return False


def _login_failed(ip):
    with _LOGIN_LOCK:
        now = time.monotonic()
        rec = _LOGIN_ATTEMPTS.get(ip)
        if not rec or now - rec.get('first', 0) > LOGIN_LOCK_SECONDS:
            rec = {'count': 1, 'first': now, 'locked_until': 0}
        else:
            rec['count'] += 1
        if rec['count'] >= MAX_LOGIN_FAILS:
            rec['locked_until'] = now + LOGIN_LOCK_SECONDS
        _LOGIN_ATTEMPTS[ip] = rec


def _login_ok(ip):
    with _LOGIN_LOCK:
        _LOGIN_ATTEMPTS.pop(ip, None)


# ================= TOTP 两步验证 =================
# 密钥存在 shares 卷里的 totp.json（重启、重建镜像都不丢）；
# 也可以用环境变量 TOTP_SECRET 直接定死（此时页面上改不了，删变量+重启可恢复）。
# 丢了验证器时的救命办法：SSH 上服务器删 data/shares/totp.json 并重启容器。
TOTP_FILE = os.path.join(SHARE_DIR, 'totp.json')
_TOTP_ENV_SECRET = os.getenv('TOTP_SECRET', '').strip() or None
_TOTP_LOCK = threading.Lock()


def _totp_load():
    if _TOTP_ENV_SECRET:
        return {'enabled': True, 'secret': _TOTP_ENV_SECRET, 'managed_by_env': True}
    try:
        with open(TOTP_FILE, encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _totp_save(data):
    tmp = TOTP_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    os.replace(tmp, TOTP_FILE)


def _totp_verify_code(secret, code):
    import pyotp
    try:
        return pyotp.TOTP(secret).verify(str(code).strip(), valid_window=1)
    except Exception:
        return False


def _totp_enabled():
    cfg = _totp_load()
    return bool(cfg.get('enabled') and cfg.get('secret'))


def _totp_verify(code):
    cfg = _totp_load()
    if not cfg.get('enabled') or not cfg.get('secret'):
        return False
    return _totp_verify_code(cfg['secret'], code)


@app.after_request
def _security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
    resp.headers['Referrer-Policy'] = 'no-referrer-when-downgrade'
    return resp


@app.errorhandler(413)
def _too_large(e):
    return jsonify({'error': '文件超过 50GB 上限'}), 413


# --- 鉴权 ---
def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

# --- 数据管理 ---
class JsonManager:
    """JSON 元数据：进程内锁 + 原子写（写临时文件再 os.replace），
    避免 Gunicorn 多进程并发写丢数据、写一半损坏。"""
    def __init__(self, filepath):
        self.filepath = filepath
        self._lock = threading.Lock()
        self.data = {}
        self.load_metadata()

    def load_metadata(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
                if not isinstance(self.data, dict):
                    self.data = {}
            except Exception:
                self.data = {}
        else:
            self.data = {}

    def save_metadata(self):
        # 调用方须持有 self._lock；写 tmp 再原子替换，失败不静默吞掉
        tmp = self.filepath + f'.tmp.{os.getpid()}'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.filepath)
        except Exception:
            app.logger.exception('写入元数据失败: %s', self.filepath)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def _reload(self):
        with self._lock:
            self.load_metadata()

class TrashManager(JsonManager):
    def add_item(self, filename, original_rel_path, is_dir):
        with self._lock:
            self.load_metadata()
            unique_name = f"{int(time.time())}_{uuid.uuid4().hex[:6]}_{filename}"
            self.data[unique_name] = {
                'original_name': filename, 'original_path': original_rel_path, 'is_dir': is_dir,
                'deleted_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'size_str': self._get_size_str(original_rel_path) if not is_dir else '-'
            }
            self.save_metadata()
            return unique_name

    def remove_item(self, unique_name):
        with self._lock:
            self.load_metadata()
            if unique_name in self.data:
                del self.data[unique_name]
                self.save_metadata()

    def get_list(self):
        with self._lock:
            self.load_metadata()
            res = []
            for uid, info in self.data.items():
                res.append({'id': uid, 'name': info['original_name'], 'path': info['original_path'], 'mtime': info['deleted_at'], 'size': info.get('size_str', '-'), 'is_dir': info['is_dir']})
            return sorted(res, key=lambda x: x['mtime'], reverse=True)

    def _get_size_str(self, rel_path):
        try:
            p = safe_join(ROOT_DIR, rel_path)
            return human_readable_size(os.path.getsize(p)) if p else '-'
        except OSError:
            return '-'

_UNSET = object()  # update_share 里“没传这个字段”的哨兵，区别于显式传 None
_PW_ITERS = 100_000
_PW_MAX_FAILS = 5
_PW_LOCK_SECONDS = 300


def _hash_share_password(password, salt_hex):
    """分享提取码 PBKDF2-HMAC-SHA256 加盐哈希，只存哈希不存明文。"""
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                               bytes.fromhex(salt_hex), _PW_ITERS).hex()


def _check_record_password(rec, password):
    """通用提取码校验：rec 须有 pw_hash/pw_salt/pw_fails/pw_locked_until 字段。
    返回 'ok' / 'wrong' / 'locked' / 'public'（无提取码），并原地更新失败计数
    （调用方负责保存）。连续错 5 次锁定 5 分钟。"""
    if not rec.get('pw_hash'):
        return 'public'
    locked_until = rec.get('pw_locked_until')
    if locked_until:
        try:
            if datetime.now() < datetime.fromisoformat(locked_until):
                return 'locked'
        except ValueError:
            pass
    ok = hmac.compare_digest(
        rec['pw_hash'],
        _hash_share_password(password or '', rec.get('pw_salt') or ''))
    if ok:
        rec['pw_fails'] = 0
        rec['pw_locked_until'] = None
        return 'ok'
    fails = rec.get('pw_fails', 0) + 1
    rec['pw_fails'] = fails
    if fails >= _PW_MAX_FAILS:
        rec['pw_fails'] = 0
        rec['pw_locked_until'] = (datetime.now() + timedelta(seconds=_PW_LOCK_SECONDS)).isoformat(timespec='seconds')
        return 'locked'
    return 'wrong'


def _set_record_password(rec, password):
    """给一条分享/收集记录设提取码：''/None=清除，字符串=设新码。"""
    if not password:
        rec['pw_salt'] = None
        rec['pw_hash'] = None
    else:
        rec['pw_salt'] = secrets.token_hex(8)
        rec['pw_hash'] = _hash_share_password(str(password), rec['pw_salt'])
    rec['pw_fails'] = 0
    rec['pw_locked_until'] = None


class ShareManager(JsonManager):
    def create_share(self, rel_path, password=None, max_downloads=None, expires_days=None):
        with self._lock:
            self.load_metadata()
            # 12 位 hex（48 bit），旧的 6 位只有 1677 万种、可爆破
            share_id = uuid.uuid4().hex[:12]
            while share_id in self.data:
                share_id = uuid.uuid4().hex[:12]
            full_path = safe_join(ROOT_DIR, rel_path)
            # expires_days：None=用全局默认；0=永不过期；>0=指定天数
            days = SHARE_EXPIRE_DAYS if expires_days is None else expires_days
            expires_at = None
            if days and days > 0:
                expires_at = (datetime.now() + timedelta(days=days)).isoformat(timespec='seconds')
            rec = {
                'file_path': rel_path, 'file_name': os.path.basename(rel_path),
                'is_dir': os.path.isdir(full_path) if full_path else False,
                'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'expires_at': expires_at, 'downloads': 0,
                'max_downloads': max_downloads if (max_downloads and max_downloads > 0) else None,
                'pw_salt': None, 'pw_hash': None, 'pw_fails': 0, 'pw_locked_until': None,
            }
            if password:
                rec['pw_salt'] = secrets.token_hex(8)
                rec['pw_hash'] = _hash_share_password(password, rec['pw_salt'])
            self.data[share_id] = rec
            self.save_metadata()
            return share_id

    def update_share(self, share_id, password=_UNSET, max_downloads=_UNSET, expires_days=_UNSET):
        """password：_UNSET=不动；''=清除提取码；字符串=设新码。
        max_downloads：_UNSET=不动；None=不限；>0=上限。
        expires_days：_UNSET=不动；0=永不过期；>0=从现在起指定天数。"""
        with self._lock:
            self.load_metadata()
            rec = self.data.get(share_id)
            if not rec:
                return False
            if password is not _UNSET:
                if not password:
                    rec['pw_salt'] = None
                    rec['pw_hash'] = None
                else:
                    rec['pw_salt'] = secrets.token_hex(8)
                    rec['pw_hash'] = _hash_share_password(str(password), rec['pw_salt'])
                rec['pw_fails'] = 0
                rec['pw_locked_until'] = None
            if max_downloads is not _UNSET:
                rec['max_downloads'] = max_downloads if (max_downloads and max_downloads > 0) else None
            if expires_days is not _UNSET:
                if expires_days and expires_days > 0:
                    rec['expires_at'] = (datetime.now() + timedelta(days=expires_days)).isoformat(timespec='seconds')
                elif expires_days == 0:
                    rec['expires_at'] = None
            self.save_metadata()
            return True

    def verify_password(self, share_id, password):
        """返回 'ok' / 'wrong' / 'locked' / 'public'（无提取码）。
        连续错 5 次锁定 5 分钟；计数存在分享记录里，4 个 worker 共享。"""
        with self._lock:
            self.load_metadata()
            rec = self.data.get(share_id)
            if not rec:
                return 'wrong'
            if not rec.get('pw_hash'):
                return 'public'
            res = _check_record_password(rec, password)
            self.save_metadata()
            return res

    @staticmethod
    def is_exhausted(info):
        limit = info.get('max_downloads')
        return bool(limit and limit > 0 and info.get('downloads', 0) >= limit)

    def get_raw(self, share_id):
        with self._lock:
            self.load_metadata()
            return self.data.get(share_id)

    def cancel_share(self, share_id):
        with self._lock:
            self.load_metadata()
            if share_id in self.data:
                del self.data[share_id]
                self.save_metadata()
                return True
            return False

    def get_list(self):
        with self._lock:
            self.load_metadata()
            res = []
            for sid, info in self.data.items():
                if _is_expired(info):
                    continue
                p = safe_join(ROOT_DIR, info['file_path'])
                exists = bool(p and os.path.exists(p))
                status = 'exhausted' if self.is_exhausted(info) else ('normal' if exists else 'lost')
                res.append({'id': sid, 'name': info['file_name'], 'path': info['file_path'],
                            'mtime': info['created_at'], 'expires_at': info.get('expires_at'),
                            'downloads': info.get('downloads', 0),
                            'max_downloads': info.get('max_downloads'),
                            'has_password': bool(info.get('pw_hash')),
                            'is_dir': bool(info.get('is_dir')),
                            'kind': 'share',
                            'status': status})
            return sorted(res, key=lambda x: x['mtime'], reverse=True)

    def get_file_info(self, share_id):
        # 过期或已达下载上限的分享视为不存在（记录保留， owner 在列表里能看到状态）
        with self._lock:
            self.load_metadata()
            info = self.data.get(share_id)
            if not info:
                return None
            if _is_expired(info):
                del self.data[share_id]
                self.save_metadata()
                return None
            if self.is_exhausted(info):
                return None
            return info

    def increment_download(self, share_id):
        with self._lock:
            self.load_metadata()
            if share_id in self.data and not _is_expired(self.data[share_id]) \
                    and not self.is_exhausted(self.data[share_id]):
                self.data[share_id]['downloads'] = self.data[share_id].get('downloads', 0) + 1
                self.save_metadata()

DROP_META_FILE = os.path.join(SHARE_DIR, 'drop_metadata.json')


class DropManager(JsonManager):
    """File Drop（收集链接）：访客无需登录，只能往指定文件夹里传文件，看不到已有内容。"""

    def create_drop(self, folder_path, title=None, password=None, max_files=None, expires_days=None):
        with self._lock:
            self.load_metadata()
            drop_id = uuid.uuid4().hex[:12]
            while drop_id in self.data:
                drop_id = uuid.uuid4().hex[:12]
            days = SHARE_EXPIRE_DAYS if expires_days is None else expires_days
            expires_at = None
            if days and days > 0:
                expires_at = (datetime.now() + timedelta(days=days)).isoformat(timespec='seconds')
            rec = {
                'folder_path': folder_path or '',
                'title': title or '',
                'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'expires_at': expires_at, 'uploads': 0,
                'max_files': max_files if (max_files and max_files > 0) else None,
                'pw_salt': None, 'pw_hash': None, 'pw_fails': 0, 'pw_locked_until': None,
            }
            _set_record_password(rec, password)
            self.data[drop_id] = rec
            self.save_metadata()
            return drop_id

    def cancel_drop(self, drop_id):
        with self._lock:
            self.load_metadata()
            if drop_id in self.data:
                del self.data[drop_id]
                self.save_metadata()
                return True
            return False

    def verify_password(self, drop_id, password):
        with self._lock:
            self.load_metadata()
            rec = self.data.get(drop_id)
            if not rec:
                return 'wrong'
            res = _check_record_password(rec, password)
            self.save_metadata()
            return res

    @staticmethod
    def is_exhausted(info):
        limit = info.get('max_files')
        return bool(limit and limit > 0 and info.get('uploads', 0) >= limit)

    def get_drop(self, drop_id):
        with self._lock:
            self.load_metadata()
            info = self.data.get(drop_id)
            if not info:
                return None
            if _is_expired(info):
                del self.data[drop_id]
                self.save_metadata()
                return None
            return info

    def increment_uploads(self, drop_id):
        with self._lock:
            self.load_metadata()
            if drop_id in self.data and not _is_expired(self.data[drop_id]) \
                    and not self.is_exhausted(self.data[drop_id]):
                self.data[drop_id]['uploads'] = self.data[drop_id].get('uploads', 0) + 1
                self.save_metadata()

    def get_list(self):
        with self._lock:
            self.load_metadata()
            res = []
            for did, info in self.data.items():
                if _is_expired(info):
                    continue
                folder = info.get('folder_path') or ''
                p = safe_join(ROOT_DIR, folder) if folder else ROOT_DIR
                exists = bool(p and os.path.isdir(p))
                status = 'exhausted' if self.is_exhausted(info) else ('normal' if exists else 'lost')
                name = info.get('title') or ('根目录' if not folder else os.path.basename(folder.rstrip('/')))
                res.append({'id': did, 'name': name, 'path': folder,
                            'mtime': info['created_at'], 'expires_at': info.get('expires_at'),
                            'downloads': info.get('uploads', 0),
                            'max_downloads': info.get('max_files'),
                            'has_password': bool(info.get('pw_hash')),
                            'is_dir': True, 'kind': 'drop',
                            'status': status})
            return sorted(res, key=lambda x: x['mtime'], reverse=True)


FILE_META_FILE = os.path.join(SHARE_DIR, 'file_meta.json')


class FileMetaManager(JsonManager):
    """文件元数据：标签 + 收藏。key 是 storage 内的相对路径。
    删进回收站时保留（还原后标签还在），彻底删除时才清掉；移动/重命名时 key 跟着走。"""

    def _clean_tags(self, tags):
        out = []
        for t in (tags or []):
            t = str(t).strip().replace('/', '').replace('\\', '')[:20]
            if t and t not in out:
                out.append(t)
            if len(out) >= 10:
                break
        return out

    def set_tags(self, rel, tags):
        with self._lock:
            self.load_metadata()
            rec = self.data.get(rel) or {}
            rec['tags'] = self._clean_tags(tags)
            if not rec['tags'] and not rec.get('fav'):
                self.data.pop(rel, None)
            else:
                self.data[rel] = rec
            self.save_metadata()
            return rec.get('tags', [])

    def toggle_fav(self, rel):
        with self._lock:
            self.load_metadata()
            rec = self.data.get(rel) or {}
            rec['fav'] = not rec.get('fav')
            if not rec['fav'] and not rec.get('tags'):
                self.data.pop(rel, None)
            else:
                self.data[rel] = rec
            self.save_metadata()
            return bool(rec.get('fav'))

    def _move_keys(self, old, new, copy=False):
        with self._lock:
            self.load_metadata()
            moved = {}
            for k, v in list(self.data.items()):
                if k == old or k.startswith(old + '/'):
                    moved[new + k[len(old):]] = v
                    if not copy:
                        del self.data[k]
            self.data.update(moved)
            self.save_metadata()

    def move_key(self, old, new):
        self._move_keys(old, new, copy=False)

    def copy_key(self, old, new):
        self._move_keys(old, new, copy=True)

    def remove_key(self, rel):
        with self._lock:
            self.load_metadata()
            for k in list(self.data.keys()):
                if k == rel or k.startswith(rel + '/'):
                    del self.data[k]
            self.save_metadata()

    def get_map(self):
        with self._lock:
            self.load_metadata()
            return dict(self.data)

    def prune(self, trash_original_paths):
        """清掉既不在 storage 也不在回收站里的元数据。"""
        with self._lock:
            self.load_metadata()
            changed = False
            for k in list(self.data.keys()):
                if os.path.exists(safe_join(ROOT_DIR, k) or '') or k in trash_original_paths:
                    continue
                del self.data[k]
                changed = True
            if changed:
                self.save_metadata()


trash_manager = TrashManager(TRASH_META_FILE)
file_meta_manager = FileMetaManager(FILE_META_FILE)
share_manager = ShareManager(SHARE_META_FILE)
drop_manager = DropManager(DROP_META_FILE)

def human_readable_size(size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"

def get_disk_usage():
    try:
        total, used, free = shutil.disk_usage(ROOT_DIR)
        return {'used': human_readable_size(used), 'total': human_readable_size(total), 'percent': (used / total) * 100}
    except OSError:
        return {'used': '0 B', 'total': '0 B', 'percent': 0}

def clean_old_archives():
    # 修复旧 bug：tasks.py 生成的是 archive_*.zip，旧代码却清 batch_download_*.zip，
    # 清理永远匹配不上；两种前缀都清，兼容历史遗留文件。
    now = time.time()
    try:
        names = os.listdir(ROOT_DIR)
    except OSError:
        return
    for f in names:
        if (f.startswith('archive_') or f.startswith('batch_download_')) and f.endswith('.zip'):
            path = os.path.join(ROOT_DIR, f)
            try:
                if os.path.isfile(path) and os.stat(path).st_mtime < now - 3600:
                    os.remove(path)
            except OSError:
                pass

# ================= 路由 =================

@app.route('/healthz')
def healthz():
    return jsonify({'status': 'ok'}), 200

@app.route('/login', methods=['GET'])
def login_page():
    if session.get('logged_in'):
        return redirect('/')
    return render_template('login.html')

@app.route('/api/login', methods=['POST'])
def login_api():
    ip = request.remote_addr or 'unknown'
    if _login_locked(ip):
        return jsonify({'status': 'fail', 'message': '失败次数过多，请 5 分钟后再试'}), 429
    data = get_json()
    if data.get('username') == ADMIN_USER and data.get('password') == ADMIN_PASS:
        # 开了 TOTP：密码对了也不直接放行，先走第二步验证码
        if _totp_enabled():
            session['pre_totp'] = True
            session.permanent = True
            return jsonify({'status': 'need_totp'})
        _login_ok(ip)
        session['logged_in'] = True
        session.permanent = True
        return jsonify({'status': 'success'})
    _login_failed(ip)
    return jsonify({'status': 'fail', 'message': '账号或密码错误'}), 401


@app.route('/api/login/totp', methods=['POST'])
def login_totp():
    """登录第二步：密码已通过（session 里有 pre_totp），再验 6 位 TOTP 码。"""
    ip = request.remote_addr or 'unknown'
    if not session.get('pre_totp'):
        return jsonify({'status': 'fail', 'message': '请先验证账号密码'}), 401
    if _login_locked(ip):
        return jsonify({'status': 'fail', 'message': '失败次数过多，请 5 分钟后再试'}), 429
    if _totp_verify(get_json().get('code', '')):
        _login_ok(ip)
        session.pop('pre_totp', None)
        session['logged_in'] = True
        session.permanent = True
        return jsonify({'status': 'success'})
    _login_failed(ip)
    return jsonify({'status': 'fail', 'message': '验证码错误'}), 401


@app.route('/api/totp/status', methods=['GET'])
@auth_required
def totp_status():
    cfg = _totp_load()
    return jsonify({'enabled': bool(cfg.get('enabled')),
                    'managed_by_env': bool(cfg.get('managed_by_env'))})


@app.route('/api/totp/setup', methods=['POST'])
@auth_required
def totp_setup():
    """生成新密钥 + 二维码（此时未启用，扫完码输一次验证码才生效）。"""
    if _totp_load().get('managed_by_env'):
        return jsonify({'error': 'TOTP 由环境变量 TOTP_SECRET 管理，无法在页面上改'}), 400
    import pyotp
    import qrcode
    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name=f'{ADMIN_USER}@cloud.sijuly.uk',
                                              issuer_name='我的云盘 Pro')
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    qr = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    with _TOTP_LOCK:
        cfg = _totp_load()
        cfg['pending_secret'] = secret
        _totp_save(cfg)
    return jsonify({'status': 'success', 'secret': secret, 'otpauth_url': uri, 'qr': qr})


@app.route('/api/totp/verify-setup', methods=['POST'])
@auth_required
def totp_verify_setup():
    if _totp_load().get('managed_by_env'):
        return jsonify({'error': 'TOTP 由环境变量管理'}), 400
    with _TOTP_LOCK:
        cfg = _totp_load()
        pending = cfg.get('pending_secret')
        if not pending:
            return jsonify({'error': '请先获取二维码'}), 400
        if _totp_verify_code(pending, get_json().get('code', '')):
            cfg['secret'] = pending
            cfg['pending_secret'] = None
            cfg['enabled'] = True
            _totp_save(cfg)
            return jsonify({'status': 'success'})
        return jsonify({'error': '验证码错误'}), 401


@app.route('/api/totp/disable', methods=['POST'])
@auth_required
def totp_disable():
    if _totp_load().get('managed_by_env'):
        return jsonify({'error': 'TOTP 由环境变量管理'}), 400
    if not _totp_verify(get_json().get('code', '')):
        return jsonify({'error': '验证码错误'}), 401
    with _TOTP_LOCK:
        cfg = _totp_load()
        cfg['secret'] = None
        cfg['pending_secret'] = None
        cfg['enabled'] = False
        _totp_save(cfg)
    return jsonify({'status': 'success'})

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect('/login')

@app.route('/')
@auth_required
def index():
    return render_template('index.html')

@app.route('/s/<share_id>', methods=['GET', 'POST'])
def public_share_link(share_id):
    if not re.fullmatch(r'[0-9a-f]{6,12}', share_id or ''):
        return "分享链接已过期或不存在", 404
    raw = share_manager.get_raw(share_id)
    if raw and ShareManager.is_exhausted(raw):
        return "该分享已达下载次数上限", 404
    info = share_manager.get_file_info(share_id)
    if not info:
        return "分享链接已过期或不存在", 404
    abs_path = safe_join(ROOT_DIR, info['file_path'])
    if not abs_path or not os.path.exists(abs_path):
        return "源文件已被删除", 404

    # 提取码门禁：验证通过后在 session 里记一笔，本次浏览器内不再重复输码
    if info.get('pw_hash') and not session.get('share_auth_' + share_id):
        err = None
        if request.method == 'POST':
            res = share_manager.verify_password(share_id, request.form.get('password', ''))
            if res in ('ok', 'public'):
                session['share_auth_' + share_id] = True
                return redirect(url_for('public_share_link', share_id=share_id))
            err = '提取码错太多次，已锁定 5 分钟，请稍后再试' if res == 'locked' else '提取码错误'
        return render_template('share.html', filename=info['file_name'],
                               need_password=True, pw_error=err)

    # 文件夹分享：列出目录内容（可进子目录），文件走 /s/<id>/f/<相对路径>
    if info['is_dir']:
        sub = (request.args.get('path') or '').replace('\\', '/').strip('/')
        target = safe_join(abs_path, sub) if sub else abs_path
        if target is None or not os.path.isdir(target):
            return "文件夹不存在", 404
        entries = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    if entry.name.startswith(('archive_', 'batch_download_')) and entry.name.endswith('.zip'):
                        continue
                    st = entry.stat()
                    rel = (sub + '/' + entry.name).strip('/') if sub else entry.name
                    entries.append({'name': entry.name, 'is_dir': entry.is_dir(),
                                    'size': human_readable_size(st.st_size) if not entry.is_dir() else '-',
                                    'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime)),
                                    'rel': rel})
        except OSError:
            pass
        entries.sort(key=lambda x: (not x['is_dir'], x['name']))
        parent = '/'.join(sub.split('/')[:-1]) if sub else None
        return render_template('share_folder.html', share_id=share_id,
                               folder_name=info['file_name'], subpath=sub, parent=parent,
                               entries=entries)

    if request.args.get('dl') == '1':
        share_manager.increment_download(share_id)
        # conditional=True：支持 Range 分段请求（视频拖进度、多线程下载的前提）
        response = send_file(abs_path, as_attachment=False, download_name=info['file_name'],
                             conditional=True)
        response.headers["Accept-Ranges"] = "bytes"
        return response

    download_url = url_for('public_share_link', share_id=share_id, dl='1')
    return render_template('share.html', filename=info['file_name'], download_url=download_url)


def _share_gate(share_id):
    """文件夹内文件/打包下载的公共门禁：返回 (info, abs_path) 或 (None, 错误文本, 状态码)。"""
    raw = share_manager.get_raw(share_id)
    if raw and ShareManager.is_exhausted(raw):
        return None, ("该分享已达下载次数上限", 404)
    info = share_manager.get_file_info(share_id)
    if not info:
        return None, ("分享链接已过期或不存在", 404)
    abs_path = safe_join(ROOT_DIR, info['file_path'])
    if not abs_path or not os.path.exists(abs_path):
        return None, ("源文件已被删除", 404)
    if info.get('pw_hash') and not session.get('share_auth_' + share_id):
        return None, ("需要提取码", 403)
    return (info, abs_path), None


@app.route('/s/<share_id>/f/<path:rel>')
def public_share_file(share_id, rel):
    if not re.fullmatch(r'[0-9a-f]{6,12}', share_id or ''):
        return "分享链接已过期或不存在", 404
    gated, err = _share_gate(share_id)
    if err:
        # 未输码的访客回到分享主页去输码，其他错误直接提示
        if err[1] == 403:
            return redirect(url_for('public_share_link', share_id=share_id))
        return err[0], err[1]
    info, folder_abs = gated
    if not info['is_dir']:
        return "分享链接已过期或不存在", 404
    target = safe_join(folder_abs, rel.replace('\\', '/'))
    if target is None or not os.path.exists(target):
        return "文件不存在", 404
    if os.path.isdir(target):
        return redirect(url_for('public_share_link', share_id=share_id, path=rel.replace('\\', '/')))
    if request.args.get('dl') == '1':
        share_manager.increment_download(share_id)
        response = send_file(target, as_attachment=False, download_name=os.path.basename(target),
                             conditional=True)
        response.headers["Accept-Ranges"] = "bytes"
        return response
    filename = os.path.basename(target)
    download_url = url_for('public_share_file', share_id=share_id, rel=rel.replace('\\', '/'), dl='1')
    return render_template('share.html', filename=filename, download_url=download_url)


@app.route('/s/<share_id>/zip')
def public_share_zip(share_id):
    """文件夹分享一键打包：整目录打成一个 zip 下载（记 1 次下载，5GB 上限防刷爆磁盘）。"""
    if not re.fullmatch(r'[0-9a-f]{6,12}', share_id or ''):
        return "分享链接已过期或不存在", 404
    gated, err = _share_gate(share_id)
    if err:
        if err[1] == 403:
            return redirect(url_for('public_share_link', share_id=share_id))
        return err[0], err[1]
    info, folder_abs = gated
    if not info['is_dir']:
        return "分享链接已过期或不存在", 404
    total = 0
    for root, _dirs, files in os.walk(folder_abs):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
            if total > 5 * 1024 * 1024 * 1024:
                return "文件夹超过 5GB，请分批下载", 400
    zip_name = f"share_{share_id}_{int(time.time())}.zip"
    zip_path = os.path.join(UPLOAD_TMP_DIR, zip_name)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(folder_abs):
            for name in files:
                full = os.path.join(root, name)
                arc = os.path.relpath(full, folder_abs)
                try:
                    zf.write(full, arc)
                except OSError:
                    pass

    @after_this_request
    def _cleanup(response):
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return response

    share_manager.increment_download(share_id)
    return send_file(zip_path, as_attachment=True, download_name=f"{info['file_name']}.zip")


# --- 图床专用接口 ---
@app.route('/img/<share_id>')
@app.route('/img/<share_id>.<ext>')
def image_hosting(share_id, ext=None):
    if not re.fullmatch(r'[0-9a-f]{6,12}', share_id or ''):
        return "404 Not Found", 404
    info = share_manager.get_file_info(share_id)
    if not info or ShareManager.is_exhausted(info):
        return "404 Not Found", 404
    # 带提取码的分享不能走图床直链（嵌在别处会绕过输码）
    if info.get('pw_hash'):
        return "404 Not Found", 404
    abs_path = safe_join(ROOT_DIR, info['file_path'])
    if not abs_path or not os.path.exists(abs_path):
        return "404 Not Found", 404
    response = send_file(abs_path, as_attachment=False, conditional=True)
    response.headers["Accept-Ranges"] = "bytes"
    return response

# --- File Drop（收集链接）：访客免登录上传，目标文件夹内容不对外展示 ---
@app.route('/d/<drop_id>', methods=['GET', 'POST'])
def public_drop(drop_id):
    if not re.fullmatch(r'[0-9a-f]{6,12}', drop_id or ''):
        return "收集链接已过期或不存在", 404
    info = drop_manager.get_drop(drop_id)
    if not info:
        return "收集链接已过期或不存在", 404
    if DropManager.is_exhausted(info):
        return "该收集链接已达文件数量上限", 404
    if info.get('pw_hash') and not session.get('drop_auth_' + drop_id):
        err = None
        if request.method == 'POST':
            res = drop_manager.verify_password(drop_id, request.form.get('password', ''))
            if res in ('ok', 'public'):
                session['drop_auth_' + drop_id] = True
                return redirect(url_for('public_drop', drop_id=drop_id))
            err = '提取码错太多次，已锁定 5 分钟，请稍后再试' if res == 'locked' else '提取码错误'
        return render_template('drop.html', title=info.get('title') or '文件收集',
                               need_password=True, pw_error=err)
    folder = info.get('folder_path') or ''
    return render_template('drop.html', title=info.get('title') or '文件收集',
                           folder_name='根目录' if not folder else os.path.basename(folder.rstrip('/')),
                           drop_id=drop_id, max_files=info.get('max_files'),
                           uploads=info.get('uploads', 0))


@app.route('/d/<drop_id>/upload', methods=['POST'])
def public_drop_upload(drop_id):
    if not re.fullmatch(r'[0-9a-f]{6,12}', drop_id or ''):
        return jsonify({'error': 'Not found'}), 404
    info = drop_manager.get_drop(drop_id)
    if not info:
        return jsonify({'error': '收集链接已过期或不存在'}), 404
    if DropManager.is_exhausted(info):
        return jsonify({'error': '已达文件数量上限'}), 400
    if info.get('pw_hash') and not session.get('drop_auth_' + drop_id):
        return jsonify({'error': '需要提取码'}), 403
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    filename = safe_name(file.filename)
    if not filename:
        return jsonify({'error': 'Invalid filename'}), 400
    folder = info.get('folder_path') or ''
    dest_dir = safe_join(ROOT_DIR, folder) if folder else ROOT_DIR
    if dest_dir is None or not os.path.isdir(dest_dir):
        return jsonify({'error': '目标文件夹不存在'}), 404
    # 重名自动加 (1)、(2)，不覆盖已有文件
    base, ext = os.path.splitext(filename)
    final, n = filename, 1
    while os.path.exists(os.path.join(dest_dir, final)):
        n += 1
        final = f"{base} ({n}){ext}"
    file.save(os.path.join(dest_dir, final))
    drop_manager.increment_uploads(drop_id)
    return jsonify({'status': 'success', 'name': final})


# --- 受保护的 API ---

@app.route('/api/list')
@auth_required
def list_files():
    req_path = request.args.get('path', '')
    abs_path = safe_join(ROOT_DIR, req_path) if req_path else ROOT_DIR
    if abs_path is None:
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(abs_path):
        return jsonify({'files': [], 'usage': get_disk_usage()})
    files = []
    meta = file_meta_manager.get_map()
    trash_paths = {v.get('original_path', '') for v in trash_manager.data.values()}
    file_meta_manager.prune(trash_paths)
    try:
        with os.scandir(abs_path) as entries:
            for entry in entries:
                # 打包产生的临时 zip 不显示在文件列表里
                if entry.name.startswith(('archive_', 'batch_download_')) and entry.name.endswith('.zip'):
                    continue
                stat = entry.stat()
                rel = os.path.join(req_path, entry.name).replace('\\', '/')
                m = meta.get(rel, {})
                files.append({'name': entry.name, 'is_dir': entry.is_dir(), 'size': human_readable_size(stat.st_size) if not entry.is_dir() else '-', 'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime)), 'path': rel, 'tags': m.get('tags', []), 'fav': bool(m.get('fav'))})
    except OSError:
        pass
    return jsonify({'files': sorted(files, key=lambda x: (not x['is_dir'], x['name'])), 'usage': get_disk_usage()})

@app.route('/api/thumb')
@auth_required
def thumb():
    """图片缩略图（懒加载）：服务端用 Pillow 生成 320px 以内 JPEG，按路径+mtime+大小缓存；
    生成失败回退原图；缓存超 3000 张时删最旧的。"""
    rel = (request.args.get('path') or '').replace('\\', '/')
    abs_path = safe_join(ROOT_DIR, rel)
    if not abs_path or not os.path.isfile(abs_path):
        return jsonify({'error': 'Not found'}), 404
    if os.path.splitext(abs_path)[1].lower() not in THUMB_EXTS:
        return jsonify({'error': 'Not an image'}), 404
    st = os.stat(abs_path)
    out = os.path.join(THUMB_DIR, _thumb_key(rel, int(st.st_mtime), st.st_size) + '.jpg')
    if not os.path.isfile(out):
        os.makedirs(THUMB_DIR, exist_ok=True)
        try:
            with Image.open(abs_path) as im:
                im = ImageOps.exif_transpose(im)  # 按 EXIF 方向摆正（手机照片常横着存）
                im.thumbnail(THUMB_SIZE, Image.LANCZOS)
                if im.mode in ('RGBA', 'LA', 'P'):
                    if im.mode == 'P':
                        im = im.convert('RGBA')
                    bg = Image.new('RGB', im.size, (255, 255, 255))
                    bg.paste(im, mask=im.split()[-1] if im.mode in ('RGBA', 'LA') else None)
                    im = bg
                elif im.mode != 'RGB':
                    im = im.convert('RGB')
                im.save(out, 'JPEG', quality=82)
            _prune_thumbs()
        except Exception:
            return send_file(abs_path, conditional=True)
    resp = send_file(out, mimetype='image/jpeg', conditional=True)
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route('/api/photos/timeline')
@auth_required
def photo_timeline():
    """照片时间线：按 EXIF 拍摄日期（没有则用文件修改时间）按月分组。
    索引存 data/shares/photo_index.json，只对新增/改动过的文件重读 EXIF。"""
    with _PHOTO_LOCK:
        _load_photo_index()
        current = {}
        for root, _dirs, files in os.walk(ROOT_DIR):
            for name in files:
                if os.path.splitext(name)[1].lower() not in THUMB_EXTS:
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, ROOT_DIR).replace('\\', '/')
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                old = _PHOTO_INDEX.get(rel)
                if old and old.get('mtime') == int(st.st_mtime) and old.get('size') == st.st_size:
                    current[rel] = old
                else:
                    date = _photo_date(full) or time.strftime('%Y-%m-%d', time.localtime(st.st_mtime))
                    current[rel] = {'date': date, 'mtime': int(st.st_mtime), 'size': st.st_size}
        if current != _PHOTO_INDEX:
            _PHOTO_INDEX.clear()
            _PHOTO_INDEX.update(current)
            _save_photo_index()
    months = {}
    for rel, info in current.items():
        month = info['date'][:7]
        months.setdefault(month, []).append(
            {'path': rel, 'name': os.path.basename(rel), 'date': info['date']})
    out = []
    for month in sorted(months, reverse=True):
        photos = sorted(months[month], key=lambda p: (p['date'], p['name']), reverse=True)
        out.append({'month': month, 'count': len(photos), 'photos': photos})
    return jsonify({'months': out})


@app.route('/api/category')
@auth_required
def list_by_category():
    category_type = request.args.get('type')
    valid_exts = set(CATEGORY_EXTENSIONS.get(category_type, []))
    files = []
    meta = file_meta_manager.get_map()
    for root, dirs, filenames in os.walk(ROOT_DIR):
        # 跳过打包临时文件所在的根目录 zip 本身在下面按名过滤
        for name in filenames:
            if name.startswith(('archive_', 'batch_download_')) and name.endswith('.zip'):
                continue
            if os.path.splitext(name)[1].lower() in valid_exts:
                abs_path = os.path.join(root, name)
                try:
                    stat = os.stat(abs_path)
                    rel = os.path.relpath(abs_path, ROOT_DIR).replace('\\', '/')
                    m = meta.get(rel, {})
                    files.append({'name': name, 'is_dir': False, 'size': human_readable_size(stat.st_size), 'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime)), 'path': rel, 'tags': m.get('tags', []), 'fav': bool(m.get('fav'))})
                except OSError:
                    pass
    return jsonify({'files': sorted(files, key=lambda x: x['mtime'], reverse=True), 'usage': get_disk_usage()})


@app.route('/api/tags/set', methods=['POST'])
@auth_required
def set_file_tags():
    data = get_json()
    rel = (data.get('path') or '').replace('\\', '/')
    abs_path = safe_join(ROOT_DIR, rel)
    if not abs_path or not os.path.exists(abs_path):
        return jsonify({'error': '文件不存在'}), 404
    tags = file_meta_manager.set_tags(rel, data.get('tags') or [])
    return jsonify({'status': 'success', 'tags': tags})


@app.route('/api/fav/toggle', methods=['POST'])
@auth_required
def toggle_fav():
    data = get_json()
    rel = (data.get('path') or '').replace('\\', '/')
    abs_path = safe_join(ROOT_DIR, rel)
    if not abs_path or not os.path.exists(abs_path):
        return jsonify({'error': '文件不存在'}), 404
    return jsonify({'status': 'success', 'fav': file_meta_manager.toggle_fav(rel)})


@app.route('/api/favorites')
@auth_required
def list_favorites():
    meta = file_meta_manager.get_map()
    files = []
    for rel, m in meta.items():
        if not m.get('fav'):
            continue
        abs_path = safe_join(ROOT_DIR, rel)
        if not abs_path or not os.path.isfile(abs_path):
            continue
        stat = os.stat(abs_path)
        files.append({'name': os.path.basename(rel), 'is_dir': False,
                      'size': human_readable_size(stat.st_size),
                      'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime)),
                      'path': rel, 'tags': m.get('tags', []), 'fav': True})
    return jsonify({'files': sorted(files, key=lambda x: x['mtime'], reverse=True),
                    'usage': get_disk_usage()})


def _file_sha256(abs_path):
    h = hashlib.sha256()
    with open(abs_path, 'rb') as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


@app.route('/api/duplicates')
@auth_required
def list_duplicates():
    """重复文件：先按大小分组，只对同大小的文件算 SHA-256（避免全库哈希）。"""
    by_size = {}
    for root, _dirs, filenames in os.walk(ROOT_DIR):
        for name in filenames:
            if name.startswith(('archive_', 'batch_download_')) and name.endswith('.zip'):
                continue
            full = os.path.join(root, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size == 0:
                continue
            by_size.setdefault(size, []).append(full)
    groups = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_hash = {}
        for full in paths:
            try:
                by_hash.setdefault(_file_sha256(full), []).append(full)
            except OSError:
                continue
        for digest, same in by_hash.items():
            if len(same) < 2:
                continue
            files = []
            for full in same:
                stat = os.stat(full)
                rel = os.path.relpath(full, ROOT_DIR).replace('\\', '/')
                files.append({'name': os.path.basename(full), 'path': rel,
                              'size': human_readable_size(stat.st_size),
                              'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime))})
            files.sort(key=lambda x: x['mtime'], reverse=True)
            groups.append({'hash': digest, 'size': human_readable_size(size),
                           'size_bytes': size, 'files': files})
    groups.sort(key=lambda g: g['size_bytes'], reverse=True)
    return jsonify({'groups': groups})

@app.route('/api/mkdir', methods=['POST'])
@auth_required
def create_folder():
    data = get_json()
    name = safe_name(data.get('name'))
    parent = safe_join(ROOT_DIR, data.get('path', '')) if data.get('path', '') else ROOT_DIR
    if not name or parent is None:
        return jsonify({'error': 'Invalid path'}), 400
    try:
        os.makedirs(os.path.join(parent, name), exist_ok=False)
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/rename', methods=['POST'])
@auth_required
def rename_item():
    data = get_json()
    src = safe_join(ROOT_DIR, data.get('path', ''))
    name = safe_name(data.get('name'))
    if not src or not name or not os.path.exists(src):
        return jsonify({'error': 'Invalid path'}), 400
    try:
        os.rename(src, os.path.join(os.path.dirname(src), name))
        new_rel = os.path.join(os.path.dirname(data.get('path', '')), name).replace('\\', '/')
        file_meta_manager.move_key(data.get('path', ''), new_rel)
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/operate', methods=['POST'])
@auth_required
def operate_items():
    data = get_json()
    action = data.get('action')
    if action not in ('move', 'copy'):
        return jsonify({'error': 'Invalid action'}), 400
    dest_abs = safe_join(ROOT_DIR, data.get('dest', '')) if data.get('dest', '') else ROOT_DIR
    if dest_abs is None or not os.path.isdir(dest_abs):
        return jsonify({'error': 'Dest not found'}), 404
    success, errors = 0, []
    for rel_path in data.get('files', []):
        try:
            src = safe_join(ROOT_DIR, rel_path)
            if not src or not os.path.exists(src):
                errors.append(f'{rel_path}: not found')
                continue
            dst = os.path.join(dest_abs, os.path.basename(rel_path))
            # 禁止把目录移/拷进自己体内（会递归爆炸）
            if os.path.isdir(src) and (dst == src or dst.startswith(src + os.sep)):
                errors.append(f'{rel_path}: 不能移入自身')
                continue
            if os.path.exists(dst):
                errors.append(f'{rel_path}: 目标已存在')
                continue
            if action == 'move':
                shutil.move(src, dst)
                new_rel = os.path.join(data.get('dest', ''), os.path.basename(rel_path)).replace('\\', '/')
                file_meta_manager.move_key(rel_path, new_rel)
            elif action == 'copy':
                shutil.copytree(src, dst) if os.path.isdir(src) else shutil.copy2(src, dst)
                new_rel = os.path.join(data.get('dest', ''), os.path.basename(rel_path)).replace('\\', '/')
                file_meta_manager.copy_key(rel_path, new_rel)
            success += 1
        except Exception as e:
            errors.append(str(e))
    return jsonify({'success': success, 'errors': errors})

@app.route('/api/share/create', methods=['POST'])
@auth_required
def create_share_link():
    data = get_json()
    files = data.get('files', [])
    password = data.get('password')
    if password is not None:
        password = str(password)[:64] or None
    max_dl = _parse_opt_int(data.get('max_downloads'))
    exp_days = _parse_opt_int(data.get('expires_days'))
    links, ids = [], []
    media_exts = set(CATEGORY_EXTENSIONS['image'] + CATEGORY_EXTENSIONS['video'])
    for p in files:
        abs_path = safe_join(ROOT_DIR, p)
        if abs_path and os.path.exists(abs_path):
            share_id = share_manager.create_share(p, password=password,
                                                  max_downloads=max_dl, expires_days=exp_days)
            filename = os.path.basename(p)
            ext = os.path.splitext(filename)[1].lower()
            # 带提取码的分享只给 /s/ 链接，不给 /img/ 直链
            if ext in media_exts and not password:
                link = f"{request.host_url}img/{share_id}{ext}"
            else:
                link = f"{request.host_url}s/{share_id}"
            links.append(link)
            ids.append(share_id)
    return jsonify({'status': 'success', 'links': links, 'ids': ids})


@app.route('/api/share/update', methods=['POST'])
@auth_required
def update_share_link():
    """改已有分享：password（''=清除提取码）、max_downloads（null=不限）、expires_days（0=永久）。"""
    data = get_json()
    sid = data.get('id')
    if not sid or not re.fullmatch(r'[0-9a-f]{6,12}', str(sid)):
        return jsonify({'error': 'Invalid id'}), 400
    pw = _UNSET
    if 'password' in data:
        pw = data.get('password')
        if pw is not None:
            pw = str(pw)[:64]
    max_dl = _UNSET
    if 'max_downloads' in data:
        max_dl = _parse_opt_int(data.get('max_downloads'))
    exp = _UNSET
    if 'expires_days' in data:
        exp = _parse_opt_int(data.get('expires_days'))
    ok = share_manager.update_share(sid, password=pw, max_downloads=max_dl, expires_days=exp)
    return jsonify({'status': 'success' if ok else 'fail'})

@app.route('/api/share/list', methods=['GET'])
@auth_required
def list_shares():
    return jsonify(share_manager.get_list())

@app.route('/api/share/cancel', methods=['POST'])
@auth_required
def cancel_share_link():
    share_manager.cancel_share(get_json().get('id'))
    return jsonify({'status': 'success'})


@app.route('/api/drop/create', methods=['POST'])
@auth_required
def create_drop_link():
    data = get_json()
    folder = (data.get('folder') or '').replace('\\', '/').strip('/')
    dest = safe_join(ROOT_DIR, folder) if folder else ROOT_DIR
    if dest is None or not os.path.isdir(dest):
        return jsonify({'error': '目标文件夹不存在'}), 404
    title = str(data.get('title') or '')[:64]
    password = data.get('password')
    if password is not None:
        password = str(password)[:64] or None
    max_files = _parse_opt_int(data.get('max_files'))
    exp_days = _parse_opt_int(data.get('expires_days'))
    drop_id = drop_manager.create_drop(folder, title=title, password=password,
                                       max_files=max_files, expires_days=exp_days)
    return jsonify({'status': 'success', 'id': drop_id,
                    'link': f"{request.host_url}d/{drop_id}"})


@app.route('/api/drop/list', methods=['GET'])
@auth_required
def list_drops():
    return jsonify(drop_manager.get_list())


@app.route('/api/drop/cancel', methods=['POST'])
@auth_required
def cancel_drop_link():
    drop_manager.cancel_drop(get_json().get('id'))
    return jsonify({'status': 'success'})

@app.route('/api/delete', methods=['POST'])
@auth_required
def soft_delete():
    count = 0
    for p in get_json().get('files', []):
        src = safe_join(ROOT_DIR, p)
        if not src or not os.path.exists(src):
            continue
        if os.path.basename(p).startswith(('batch_download_', 'archive_')) and p.endswith('.zip'):
            try:
                os.remove(src)
            except OSError:
                pass
            continue
        uid = trash_manager.add_item(os.path.basename(p), p, os.path.isdir(src))
        try:
            shutil.move(src, os.path.join(TRASH_DIR, uid))
            count += 1
        except OSError:
            pass
    return jsonify({'status': 'success', 'count': count})

@app.route('/api/trash/list')
@auth_required
def get_trash_list():
    return jsonify(trash_manager.get_list())

@app.route('/api/trash/restore', methods=['POST'])
@auth_required
def restore_trash():
    count = 0
    for uid in get_json().get('items', []):
        if not re.fullmatch(r'[0-9]+_[0-9a-f]{6}_.+', uid or ''):
            continue
        info = trash_manager.data.get(uid)
        if info:
            # 还原目标必须在 storage 内；原目录没了就还原到根目录
            tgt = safe_join(ROOT_DIR, info['original_path'])
            if tgt is None:
                tgt = safe_join(ROOT_DIR, info['original_name'])
            if not tgt:
                continue
            if not os.path.exists(os.path.dirname(tgt)):
                tgt = os.path.join(ROOT_DIR, info['original_name'])
            try:
                shutil.move(os.path.join(TRASH_DIR, uid), tgt)
                trash_manager.remove_item(uid)
                count += 1
            except OSError:
                pass
    return jsonify({'status': 'success', 'count': count})

@app.route('/api/trash/delete', methods=['POST'])
@auth_required
def permanent_delete():
    for uid in get_json().get('items', []):
        if not re.fullmatch(r'[0-9]+_[0-9a-f]{6}_.+', uid or ''):
            continue
        p = os.path.join(TRASH_DIR, uid)
        # uid 已校验格式，且 trash 目录内的条目名就是 uid 本身，不拼接用户路径
        try:
            info = trash_manager.data.get(uid)
            (shutil.rmtree(p) if os.path.isdir(p) else os.remove(p))
            trash_manager.remove_item(uid)
            if info:
                file_meta_manager.remove_key(info.get('original_path', ''))
        except OSError:
            pass
    return jsonify({'status': 'success'})

@app.route('/api/trash/empty', methods=['POST'])
@auth_required
def empty_trash():
    for uid in list(trash_manager.data.keys()):
        p = os.path.join(TRASH_DIR, uid)
        try:
            info = trash_manager.data.get(uid)
            (shutil.rmtree(p) if os.path.isdir(p) else os.remove(p))
            trash_manager.remove_item(uid)
            if info:
                file_meta_manager.remove_key(info.get('original_path', ''))
        except OSError:
            pass
    return jsonify({'status': 'success'})

@app.route('/api/upload', methods=['POST'])
@auth_required
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    filename = safe_name(file.filename)
    if not filename:
        return jsonify({'error': 'Invalid filename'}), 400
    save_dir = safe_join(ROOT_DIR, request.form.get('path', '')) if request.form.get('path', '') else ROOT_DIR
    if save_dir is None:
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
    file.save(os.path.join(save_dir, filename))
    return jsonify({'status': 'success'})


# ================= 分片上传 + 断点续传 =================
# 流程：init（拿 upload_id + 已传分片）-> chunk（并行传分片，可重试/续传）
#      -> complete（服务端按序合并校验）-> status/cancel 辅助接口。
# 分片 5MB 一片（前端定），单文件 50GB 上限不变；旧的单次 /api/upload 保留兼容。

@app.route('/api/upload/init', methods=['POST'])
@auth_required
def upload_init():
    data = get_json()
    filename = safe_name(data.get('name', ''))
    size = data.get('size')
    chunk_size = data.get('chunk_size')
    total_chunks = data.get('total_chunks')
    if (not filename or not isinstance(size, int) or size <= 0
            or size > app.config['MAX_CONTENT_LENGTH']
            or not isinstance(chunk_size, int) or not 1024 * 1024 <= chunk_size <= 64 * 1024 * 1024
            or not isinstance(total_chunks, int) or not 1 <= total_chunks <= MAX_CHUNKS
            or total_chunks != (size + chunk_size - 1) // chunk_size):
        return jsonify({'error': 'Invalid upload params'}), 400
    rel_path = data.get('path', '') or ''
    save_dir = safe_join(ROOT_DIR, rel_path) if rel_path else ROOT_DIR
    if save_dir is None:
        return jsonify({'error': 'Invalid path'}), 400
    _cleanup_stale_uploads()
    # 断点续传：前端带回上次的 upload_id 且元数据一致时复用，已传分片不用重传
    upload_id = data.get('upload_id') or ''
    meta = _upload_meta(upload_id) if upload_id else None
    if not (meta and meta.get('name') == filename and meta.get('size') == size
            and meta.get('chunk_size') == chunk_size
            and meta.get('total_chunks') == total_chunks
            and meta.get('path') == rel_path):
        upload_id = uuid.uuid4().hex
        meta = {'name': filename, 'path': rel_path, 'size': size,
                'chunk_size': chunk_size, 'total_chunks': total_chunks,
                'created': time.time()}
        os.makedirs(_upload_dir(upload_id), exist_ok=True)
        with open(os.path.join(_upload_dir(upload_id), 'meta.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f)
    return jsonify({'upload_id': upload_id,
                    'uploaded': _uploaded_chunks(upload_id, total_chunks)})


@app.route('/api/upload/chunk', methods=['POST'])
@auth_required
def upload_chunk():
    upload_id = request.form.get('upload_id', '')
    meta = _upload_meta(upload_id)
    if not meta:
        return jsonify({'error': 'Invalid upload_id'}), 400
    try:
        index = int(request.form.get('index', -1))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid index'}), 400
    if not 0 <= index < meta['total_chunks'] or 'chunk' not in request.files:
        return jsonify({'error': 'Invalid chunk'}), 400
    chunk = request.files['chunk']
    data = chunk.read()
    expected = meta['chunk_size'] if index < meta['total_chunks'] - 1 \
        else meta['size'] - meta['chunk_size'] * (meta['total_chunks'] - 1)
    if len(data) != expected:
        return jsonify({'error': 'Chunk size mismatch'}), 400
    d = _upload_dir(upload_id)
    tmp = os.path.join(d, f'{index}.tmp')
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, os.path.join(d, f'{index}.part'))
    return jsonify({'status': 'success', 'index': index})


@app.route('/api/upload/status')
@auth_required
def upload_status():
    upload_id = request.args.get('upload_id', '')
    meta = _upload_meta(upload_id)
    if not meta:
        return jsonify({'error': 'Invalid upload_id'}), 400
    return jsonify({'upload_id': upload_id,
                    'uploaded': _uploaded_chunks(upload_id, meta['total_chunks']),
                    'total_chunks': meta['total_chunks']})


@app.route('/api/upload/complete', methods=['POST'])
@auth_required
def upload_complete():
    upload_id = get_json().get('upload_id', '')
    meta = _upload_meta(upload_id)
    if not meta:
        return jsonify({'error': 'Invalid upload_id'}), 400
    uploaded = _uploaded_chunks(upload_id, meta['total_chunks'])
    if len(uploaded) != meta['total_chunks']:
        return jsonify({'error': 'Missing chunks',
                        'missing': sorted(set(range(meta['total_chunks'])) - set(uploaded))}), 400
    rel_path = meta['path']
    save_dir = safe_join(ROOT_DIR, rel_path) if rel_path else ROOT_DIR
    if save_dir is None:
        return jsonify({'error': 'Invalid path'}), 400
    os.makedirs(save_dir, exist_ok=True)
    final_path = os.path.join(save_dir, meta['name'])
    d = _upload_dir(upload_id)
    try:
        with open(final_path, 'wb') as out:
            for i in range(meta['total_chunks']):
                with open(os.path.join(d, f'{i}.part'), 'rb') as part:
                    shutil.copyfileobj(part, out, length=1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        if os.path.getsize(final_path) != meta['size']:
            os.remove(final_path)
            return jsonify({'error': 'Size mismatch after merge'}), 400
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return jsonify({'status': 'success'})


@app.route('/api/upload/cancel', methods=['POST'])
@auth_required
def upload_cancel():
    upload_id = get_json().get('upload_id', '')
    d = _upload_dir(upload_id)
    if d:
        shutil.rmtree(d, ignore_errors=True)
    return jsonify({'status': 'success'})


@app.route('/api/archive', methods=['POST'])
@auth_required
def archive_files():
    clean_old_archives()
    from tasks import compress_files_task
    abs_paths = []
    for p in get_json().get('files', []):
        ap = safe_join(ROOT_DIR, p)
        if ap and os.path.exists(ap):
            abs_paths.append(ap)
    if not abs_paths:
        return jsonify({'error': 'No valid files'}), 400
    task = compress_files_task.delay(abs_paths)
    return jsonify({'task_id': task.id, 'status': 'Processing'})

@app.route('/api/status/<task_id>')
@auth_required
def task_status(task_id):
    if not re.fullmatch(r'[0-9a-f-]{36}', task_id or ''):
        return jsonify({'error': 'Invalid task id'}), 400
    from tasks import compress_files_task
    task = compress_files_task.AsyncResult(task_id)
    res = {'state': task.state}
    if task.state == 'PROGRESS':
        res.update(task.info)
    elif task.state == 'SUCCESS':
        res['result'] = task.result
    elif task.state == 'FAILURE':
        res['error'] = str(task.result)
    return jsonify(res)

@app.route('/api/download_result')
@auth_required
def download_result_file():
    # 修复任意文件读取：只允许 storage 内的 archive_*/batch_download_*.zip，
    # 拒绝 /etc/passwd、绝对路径、.. 穿越；这是旧接口最大的洞。
    file_path = request.args.get('file', '')
    resolved = os.path.realpath(file_path)
    root = os.path.realpath(ROOT_DIR)
    name = os.path.basename(resolved)
    if (not resolved.startswith(root + os.sep)
            or not name.endswith('.zip')
            or not (name.startswith('archive_') or name.startswith('batch_download_'))
            or not os.path.isfile(resolved)):
        return jsonify({'error': 'Invalid file'}), 400
    return send_file(resolved, as_attachment=True, download_name=name, conditional=True)

@app.route('/api/file')
@auth_required
def get_file_content():
    abs_path = safe_join(ROOT_DIR, request.args.get('path', ''))
    if not abs_path or not os.path.isfile(abs_path):
        return jsonify({'error': 'File not found'}), 404
    # conditional=True：Werkzeug 处理 Range 请求回 206 + Content-Range，
    # 视频在线拖进度、浏览器断点续传、多线程分段下载都靠它。
    response = send_file(abs_path, conditional=True)
    response.headers["Accept-Ranges"] = "bytes"
    return response

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
