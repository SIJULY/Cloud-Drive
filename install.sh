#!/bin/bash
# My Cloud Pro Max 一键安装 / 更新 / 卸载
# 修复记录：REPO_URL 原来写成了不存在的 SIJULY/cloud.git，导致 README 的一键安装
# 根本拉不到代码；已改成 SIJULY/Cloud-Drive.git。
# 凭据不再写死 admin123、不再嵌进 docker-compose.yml，改为 .env + 随机生成。

set -u

GREEN="\033[32m"
RED="\033[31m"
YELLOW="\033[33m"
PLAIN="\033[0m"

APP_NAME="my-cloud-drive"
INSTALL_DIR="/opt/$APP_NAME"
REPO_URL="https://github.com/SIJULY/Cloud-Drive.git"

# docker compose v2 优先，旧版 docker-compose 兜底
compose() {
    if docker compose version >/dev/null 2>&1; then docker compose "$@";
    elif command -v docker-compose >/dev/null 2>&1; then docker-compose "$@";
    else echo -e "${RED}未找到 docker compose${PLAIN}"; exit 1; fi
}

check_root() {
    if [[ $EUID -ne 0 ]]; then
        echo -e "${RED}错误: 请使用 sudo 或 root 权限运行此脚本${PLAIN}"
        exit 1
    fi
}

check_env() {
    if ! command -v git &> /dev/null; then
        echo -e "${YELLOW}正在安装 Git...${PLAIN}"
        if [ -x "$(command -v apt-get)" ]; then apt-get update && apt-get install -y git
        elif [ -x "$(command -v yum)" ]; then yum install -y git
        else echo -e "${RED}无法自动安装 git${PLAIN}"; exit 1; fi
    fi
    if ! command -v docker &> /dev/null; then
        echo -e "${YELLOW}正在安装 Docker...${PLAIN}"
        curl -fsSL https://get.docker.com | bash
        systemctl enable docker; systemctl start docker
    fi
    if ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
        echo -e "${YELLOW}正在安装 Docker Compose...${PLAIN}"
        curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
        chmod +x /usr/local/bin/docker-compose
    fi
}

# 生成 .env：用户回车留空时用随机值，绝不 fallback 到 admin123
write_env() {
    local admin_user="$1" admin_pass="$2"
    if [ -z "$admin_pass" ]; then admin_pass="$(openssl rand -hex 12)"; echo -e "${YELLOW}密码留空，已随机生成，见下方输出，请记好${PLAIN}"; fi
    cat > .env <<EOF
ADMIN_USER=${admin_user}
ADMIN_PASS=${admin_pass}
SECRET_KEY=$(openssl rand -hex 32)
SHARE_EXPIRE_DAYS=30
EOF
    chmod 600 .env
    echo "$admin_pass"
}

install_app() {
    check_root; check_env
    echo -e "${GREEN}=== 开始安装 ${APP_NAME} ===${PLAIN}"

    if [ -d "$INSTALL_DIR" ]; then
        echo -e "${YELLOW}清理旧安装（保留 data/ 数据，凭据 .env 会被新生成的覆盖前先备份）...${PLAIN}"
        cd "$INSTALL_DIR" && compose down >/dev/null 2>&1 || true
        [ -f .env ] && cp .env /tmp/my-cloud-drive.env.bak
        cd ..
        rm -rf "$INSTALL_DIR"
    fi

    echo -e "${GREEN}拉取源码...${PLAIN}"
    git clone "$REPO_URL" "$INSTALL_DIR"
    if [ ! -d "$INSTALL_DIR" ]; then echo -e "${RED}拉取失败${PLAIN}"; exit 1; fi
    cd "$INSTALL_DIR"

    read -p "设置管理员用户名 (默认: admin): " ADMIN_USER; ADMIN_USER=${ADMIN_USER:-admin}
    read -p "设置管理员密码 (留空则随机生成): " ADMIN_PASS
    ADMIN_PASS=$(write_env "$ADMIN_USER" "$ADMIN_PASS")

    echo -e "访问方式:\n1. IP + 端口\n2. 域名 (HTTPS)"
    read -p "选择 [1-2]: " MODE

    PROXY_CONFIG="reverse_proxy app:5000 {
        transport http {
            read_timeout 300s
            write_timeout 300s
        }
    }"
    BODY_CONFIG="request_body {
        max_size 50GB
    }"

    if [[ "$MODE" == "2" ]]; then
        read -p "输入域名: " DOMAIN
        printf '%s {\n    encode gzip\n    %s\n    %s\n}\n' "$DOMAIN" "$BODY_CONFIG" "$PROXY_CONFIG" > caddy/Caddyfile
        CADDY_PORTS='      - "80:80"
      - "443:443"'
    else
        read -p "输入端口 (默认: 8080): " PORT; PORT=${PORT:-8080}
        printf ':%s {\n    encode gzip\n    %s\n    %s\n}\n' "$PORT" "$BODY_CONFIG" "$PROXY_CONFIG" > caddy/Caddyfile
        # IP 模式只暴露这一个端口，不再顺带开 80/443
        CADDY_PORTS="      - \"${PORT}:${PORT}\""
    fi

    # 只改 Caddy 的端口映射，不再重写整个 docker-compose.yml（凭据在 .env 里）
    if [ -n "${CADDY_PORTS:-}" ]; then
        python3 - "$CADDY_PORTS" <<'PYEOF'
import re, sys
ports = sys.argv[1]
p = 'docker-compose.yml'
s = open(p).read()
# 替换 caddy 服务下的 ports 段（两个端口行）
s = re.sub(r'(caddy:\n(?:.*\n)*?    ports:\n)(?:.*\n)*?(    volumes:)',
           lambda m: m.group(1) + ports + "\n" + m.group(2), s, count=1)
open(p, 'w').write(s)
PYEOF
    fi

    echo -e "${YELLOW}构建并启动...${PLAIN}"
    compose up -d --build

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}安装成功！${PLAIN}"
        if [[ "$MODE" == "2" ]]; then echo -e "访问: https://$DOMAIN"
        else echo -e "访问: http://$(curl -s4 ifconfig.me):$PORT"; fi
        echo -e "账号: $ADMIN_USER / 密码: $ADMIN_PASS"
        echo -e "${YELLOW}凭据已写入 $INSTALL_DIR/.env（权限 600），请妥善保管${PLAIN}"
    else
        echo -e "${RED}安装失败${PLAIN}"
    fi
}

update_app() {
    check_root
    [ ! -d "$INSTALL_DIR" ] && echo -e "${RED}未安装${PLAIN}" && exit 1
    cd "$INSTALL_DIR"
    # .env、数据和 Caddyfile 都保留，只更新代码
    cp caddy/Caddyfile caddy/Caddyfile.bak
    git fetch --all; git reset --hard origin/main
    mv caddy/Caddyfile.bak caddy/Caddyfile
    compose up -d --build --remove-orphans
    docker image prune -f
    echo -e "${GREEN}更新完成${PLAIN}"
}

uninstall_app() {
    check_root
    read -p "确定要卸载吗？所有数据（包括网盘文件和Caddy配置）都将被删除！(y/n): " CONFIRM
    if [[ "$CONFIRM" == "y" ]]; then
        if [ -d "$INSTALL_DIR" ]; then
            cd "$INSTALL_DIR"
            echo -e "${YELLOW}停止容器...${PLAIN}"
            compose down
            cd ..
            echo -e "${YELLOW}清理文件...${PLAIN}"
            rm -rf "$INSTALL_DIR"
            echo -e "${GREEN}卸载完成，所有相关文件已彻底清理。${PLAIN}"
        else
            echo -e "${RED}目录不存在${PLAIN}"
        fi
    else
        echo "已取消"
    fi
}

echo -e "1. 安装"
echo -e "2. 更新"
echo -e "3. 卸载"
read -p "选择: " CHOICE
case $CHOICE in
    1) install_app ;; 2) update_app ;; 3) uninstall_app ;; *) echo "无效" ;;
esac
