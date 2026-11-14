#!/usr/bin/env python3
"""aria2 下载完成/出错钩子（--on-download-complete / --on-download-error 调用）。

aria2 传参：$1=gid，$2=文件数，$3=第一个文件的路径。
本脚本只做两件事：把下好的文件移进网盘目标文件夹，并把 offline.json 里
对应任务的状态改成 done/failed。失败不抛错、不阻塞 aria2。
注意：网盘主程序（JsonManager）不认文件锁，这里只做“读-改-原子写”，
和用户在网页端点取消同时发生时，后写的一方为准——个人网盘可接受。
"""
import json
import os
import shutil
import sys
import urllib.request

SHARE_DIR = os.environ.get('SHARE_PATH', '/app/shares')
STORAGE_DIR = os.environ.get('STORAGE_PATH', '/app/storage')
DOWNLOAD_DIR = os.path.join(SHARE_DIR, 'downloads')
OFFLINE_FILE = os.path.join(SHARE_DIR, 'offline.json')
RPC_URL = 'http://127.0.0.1:6800/jsonrpc'
SECRET = os.environ.get('ARIA2_SECRET') or os.environ.get('SECRET_KEY', '')
QUOTA_BYTES = int(float(os.environ.get('QUOTA_GB', '0') or 0) * 1024 ** 3)


def rpc(method, params=None):
    payload = json.dumps({'jsonrpc': '2.0', 'id': 'hook',
                          'method': method,
                          'params': [f'token:{SECRET}'] + (params or [])}).encode()
    req = urllib.request.Request(RPC_URL, data=payload, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        out = json.loads(resp.read())
    if 'error' in out:
        raise RuntimeError(out['error'].get('message', 'aria2 错误'))
    return out.get('result')


def load_tasks():
    try:
        with open(OFFLINE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {'tasks': []}


def save_tasks(data):
    tmp = OFFLINE_FILE + '.hook.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OFFLINE_FILE)


def storage_used():
    total = 0
    for dirpath, _dirs, files in os.walk(STORAGE_DIR):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while True:
        n += 1
        cand = f"{base} ({n}){ext}"
        if not os.path.exists(cand):
            return cand


def main():
    if len(sys.argv) < 2:
        return
    gid = sys.argv[1]
    try:
        st = rpc('aria2.tellStatus', [gid])
    except Exception:
        return
    status = st.get('status')
    data = load_tasks()
    task = next((t for t in data.get('tasks', []) if t.get('gid') == gid), None)
    if task is None:
        return
    if status != 'complete':
        task['status'] = 'error'
        task['error'] = st.get('errorMessage') or f'aria2 状态: {status}'
        save_tasks(data)
        return
    dest_rel = (task.get('dest') or '').strip('/')
    dest_dir = os.path.join(STORAGE_DIR, dest_rel) if dest_rel else STORAGE_DIR
    os.makedirs(dest_dir, exist_ok=True)
    files = st.get('files') or []
    paths = [f['path'] for f in files if f.get('path')]
    if not paths:
        task['status'] = 'error'
        task['error'] = 'aria2 没返回文件'
        save_tasks(data)
        return
    total_size = sum(int(f.get('length') or 0) for f in files)
    if QUOTA_BYTES > 0 and storage_used() + total_size > QUOTA_BYTES:
        task['status'] = 'error'
        task['error'] = '配额已满，文件留在下载目录'
        save_tasks(data)
        return
    # 多文件下载（种子）：把共同的顶层目录整个移过去；单文件直接移文件
    rel_paths = [os.path.relpath(p, DOWNLOAD_DIR) for p in paths]
    tops = {p.split(os.sep)[0] for p in rel_paths}
    try:
        if len(tops) == 1 and len(rel_paths) > 1:
            src = os.path.join(DOWNLOAD_DIR, next(iter(tops)))
            shutil.move(src, unique_path(os.path.join(dest_dir, os.path.basename(src))))
        else:
            for p in paths:
                shutil.move(p, unique_path(os.path.join(dest_dir, os.path.basename(p))))
    except OSError as e:
        task['status'] = 'error'
        task['error'] = f'移入网盘失败: {e}'
        save_tasks(data)
        return
    task['status'] = 'done'
    task['progress'] = 100
    save_tasks(data)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass
