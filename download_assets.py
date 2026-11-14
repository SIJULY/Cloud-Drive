import os
import sys
import urllib.request
import time

# 要下载的文件和本地保存路径（templates 里引用的是这些本地路径，不再走 CDN）
assets = [
    {
        "urls": [
            "https://cdn.staticfile.org/vue/3.3.4/vue.global.prod.min.js",
            "https://unpkg.com/vue@3.3.4/dist/vue.global.prod.js",
        ],
        "folder": "static/js",
        "name": "vue.js",
    },
    {
        "urls": [
            "https://cdn.staticfile.org/tailwindcss/2.2.19/tailwind.min.css",
            "https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css",
        ],
        "folder": "static/css",
        "name": "tailwind.css",
    },
    {
        "urls": [
            "https://cdn.staticfile.org/font-awesome/6.4.0/css/all.min.css",
            "https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.4.0/css/all.min.css",
        ],
        "folder": "static/css",
        "name": "fontawesome.css",
    },
    # FontAwesome 的 CSS 只引用字体文件，不含字形本身：all.min.css 里
    # @font-face 指向 ../webfonts/fa-*.woff2。旧版只下了 CSS 没下字体，
    # 导致线上所有图标 404、纯图标按钮（返回/列表切换/刷新）全是空白。
    # 字体文件较小（woff2 即可，现代浏览器都支持），校验下限放宽到 1KB。
    {
        "urls": [
            "https://cdn.staticfile.org/font-awesome/6.4.0/webfonts/fa-solid-900.woff2",
            "https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.4.0/webfonts/fa-solid-900.woff2",
        ],
        "folder": "static/webfonts",
        "name": "fa-solid-900.woff2",
        "min_size": 1024,
    },
    {
        "urls": [
            "https://cdn.staticfile.org/font-awesome/6.4.0/webfonts/fa-regular-400.woff2",
            "https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.4.0/webfonts/fa-regular-400.woff2",
        ],
        "folder": "static/webfonts",
        "name": "fa-regular-400.woff2",
        "min_size": 1024,
    },
    {
        "urls": [
            "https://cdn.staticfile.org/font-awesome/6.4.0/webfonts/fa-brands-400.woff2",
            "https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.4.0/webfonts/fa-brands-400.woff2",
        ],
        "folder": "static/webfonts",
        "name": "fa-brands-400.woff2",
        "min_size": 1024,
    },
]

base_dir = os.path.dirname(os.path.abspath(__file__))

print("Resource Download: Start...")

failed = []
for item in assets:
    folder_path = os.path.join(base_dir, item["folder"])
    os.makedirs(folder_path, exist_ok=True)
    file_path = os.path.join(folder_path, item["name"])

    ok = False
    for url in item["urls"]:
        for attempt in range(3):
            try:
                print(f"Downloading {item['name']} from {url} (Attempt {attempt + 1})...")
                urllib.request.urlretrieve(url, file_path)
                # 校验：空文件 / 错误页都算失败，继续试下一个源
                if os.path.getsize(file_path) < item.get("min_size", 1024):
                    raise ValueError(f"downloaded file too small: {os.path.getsize(file_path)} bytes")
                print(f"Saved to: {item['folder']}/{item['name']}")
                ok = True
                break
            except Exception as e:
                print(f"Error downloading {item['name']} from {url}: {e}")
                time.sleep(1)
        if ok:
            break
    if not ok:
        failed.append(item["name"])

if failed:
    # 构建时就失败，别带着缺资源的镜像上线
    print(f"Resource Download: FAILED for {', '.join(failed)}", file=sys.stderr)
    sys.exit(1)

print("Resource Download: Completed!")
