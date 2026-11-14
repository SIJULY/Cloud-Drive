import os
import re
import secrets
import shutil
import json
import time
import threading
import uuid
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, jsonify, request, send_file, redirect, session, url_for
from werkzeug.utils import secure_filename

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

class ShareManager(JsonManager):
    def create_share(self, rel_path):
        with self._lock:
            self.load_metadata()
            # 12 位 hex（48 bit），旧的 6 位只有 1677 万种、可爆破
            share_id = uuid.uuid4().hex[:12]
            while share_id in self.data:
                share_id = uuid.uuid4().hex[:12]
            full_path = safe_join(ROOT_DIR, rel_path)
            expires_at = None
            if SHARE_EXPIRE_DAYS > 0:
                expires_at = (datetime.now() + timedelta(days=SHARE_EXPIRE_DAYS)).isoformat(timespec='seconds')
            self.data[share_id] = {
                'file_path': rel_path, 'file_name': os.path.basename(rel_path),
                'is_dir': os.path.isdir(full_path) if full_path else False,
                'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'expires_at': expires_at, 'downloads': 0
            }
            self.save_metadata()
            return share_id

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
                res.append({'id': sid, 'name': info['file_name'], 'path': info['file_path'],
                            'mtime': info['created_at'], 'expires_at': info.get('expires_at'),
                            'downloads': info.get('downloads', 0),
                            'status': 'normal' if exists else 'lost'})
            return sorted(res, key=lambda x: x['mtime'], reverse=True)

    def get_file_info(self, share_id):
        # 过期分享视为不存在，并顺手清理
        with self._lock:
            self.load_metadata()
            info = self.data.get(share_id)
            if not info:
                return None
            if _is_expired(info):
                del self.data[share_id]
                self.save_metadata()
                return None
            return info

    def increment_download(self, share_id):
        with self._lock:
            self.load_metadata()
            if share_id in self.data and not _is_expired(self.data[share_id]):
                self.data[share_id]['downloads'] = self.data[share_id].get('downloads', 0) + 1
                self.save_metadata()

trash_manager = TrashManager(TRASH_META_FILE)
share_manager = ShareManager(SHARE_META_FILE)

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
        _login_ok(ip)
        session['logged_in'] = True
        session.permanent = True
        return jsonify({'status': 'success'})
    _login_failed(ip)
    return jsonify({'status': 'fail', 'message': '账号或密码错误'}), 401

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect('/login')

@app.route('/')
@auth_required
def index():
    return render_template('index.html')

@app.route('/s/<share_id>')
def public_share_link(share_id):
    if not re.fullmatch(r'[0-9a-f]{6,12}', share_id or ''):
        return "分享链接已过期或不存在", 404
    info = share_manager.get_file_info(share_id)
    if not info:
        return "分享链接已过期或不存在", 404
    abs_path = safe_join(ROOT_DIR, info['file_path'])
    if not abs_path or not os.path.exists(abs_path):
        return "源文件已被删除", 404
    if info['is_dir']:
        return f"这是一个文件夹 ({info['file_name']})，暂不支持下载。", 200

    if request.args.get('dl') == '1':
        share_manager.increment_download(share_id)
        # conditional=True：支持 Range 分段请求（视频拖进度、多线程下载的前提）
        response = send_file(abs_path, as_attachment=False, download_name=info['file_name'],
                             conditional=True)
        response.headers["Accept-Ranges"] = "bytes"
        return response

    download_url = url_for('public_share_link', share_id=share_id, dl='1')
    return render_template('share.html', filename=info['file_name'], download_url=download_url)


# --- 图床专用接口 ---
@app.route('/img/<share_id>')
@app.route('/img/<share_id>.<ext>')
def image_hosting(share_id, ext=None):
    if not re.fullmatch(r'[0-9a-f]{6,12}', share_id or ''):
        return "404 Not Found", 404
    info = share_manager.get_file_info(share_id)
    if not info:
        return "404 Not Found", 404
    abs_path = safe_join(ROOT_DIR, info['file_path'])
    if not abs_path or not os.path.exists(abs_path):
        return "404 Not Found", 404
    response = send_file(abs_path, as_attachment=False, conditional=True)
    response.headers["Accept-Ranges"] = "bytes"
    return response

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
    try:
        with os.scandir(abs_path) as entries:
            for entry in entries:
                # 打包产生的临时 zip 不显示在文件列表里
                if entry.name.startswith(('archive_', 'batch_download_')) and entry.name.endswith('.zip'):
                    continue
                stat = entry.stat()
                files.append({'name': entry.name, 'is_dir': entry.is_dir(), 'size': human_readable_size(stat.st_size) if not entry.is_dir() else '-', 'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime)), 'path': os.path.join(req_path, entry.name).replace('\\', '/')})
    except OSError:
        pass
    return jsonify({'files': sorted(files, key=lambda x: (not x['is_dir'], x['name'])), 'usage': get_disk_usage()})

@app.route('/api/category')
@auth_required
def list_by_category():
    category_type = request.args.get('type')
    valid_exts = set(CATEGORY_EXTENSIONS.get(category_type, []))
    files = []
    for root, dirs, filenames in os.walk(ROOT_DIR):
        # 跳过打包临时文件所在的根目录 zip 本身在下面按名过滤
        for name in filenames:
            if name.startswith(('archive_', 'batch_download_')) and name.endswith('.zip'):
                continue
            if os.path.splitext(name)[1].lower() in valid_exts:
                abs_path = os.path.join(root, name)
                try:
                    stat = os.stat(abs_path)
                    files.append({'name': name, 'is_dir': False, 'size': human_readable_size(stat.st_size), 'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime)), 'path': os.path.relpath(abs_path, ROOT_DIR).replace('\\', '/')})
                except OSError:
                    pass
    return jsonify({'files': sorted(files, key=lambda x: x['mtime'], reverse=True), 'usage': get_disk_usage()})

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
            elif action == 'copy':
                shutil.copytree(src, dst) if os.path.isdir(src) else shutil.copy2(src, dst)
            success += 1
        except Exception as e:
            errors.append(str(e))
    return jsonify({'success': success, 'errors': errors})

@app.route('/api/share/create', methods=['POST'])
@auth_required
def create_share_link():
    data = get_json()
    files = data.get('files', [])
    links = []
    media_exts = set(CATEGORY_EXTENSIONS['image'] + CATEGORY_EXTENSIONS['video'])
    for p in files:
        abs_path = safe_join(ROOT_DIR, p)
        if abs_path and os.path.exists(abs_path):
            share_id = share_manager.create_share(p)
            filename = os.path.basename(p)
            ext = os.path.splitext(filename)[1].lower()
            if ext in media_exts:
                link = f"{request.host_url}img/{share_id}{ext}"
            else:
                link = f"{request.host_url}s/{share_id}"
            links.append(link)
    return jsonify({'status': 'success', 'links': links})

@app.route('/api/share/list', methods=['GET'])
@auth_required
def list_shares():
    return jsonify(share_manager.get_list())

@app.route('/api/share/cancel', methods=['POST'])
@auth_required
def cancel_share_link():
    share_manager.cancel_share(get_json().get('id'))
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
            (shutil.rmtree(p) if os.path.isdir(p) else os.remove(p))
            trash_manager.remove_item(uid)
        except OSError:
            pass
    return jsonify({'status': 'success'})

@app.route('/api/trash/empty', methods=['POST'])
@auth_required
def empty_trash():
    for uid in list(trash_manager.data.keys()):
        p = os.path.join(TRASH_DIR, uid)
        try:
            (shutil.rmtree(p) if os.path.isdir(p) else os.remove(p))
            trash_manager.remove_item(uid)
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
