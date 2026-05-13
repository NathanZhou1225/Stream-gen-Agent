#!/usr/bin/env bash
set -euo pipefail

# 脚本功能：下载远端的 zip 文件，解压后执行 aime-install.sh 命令

# 默认配置
DEFAULT_ZIP_URL="https://www.iwencai.com/skillhub/static/0.0.4/iwencai-skillhub-cli.zip"
DEFAULT_TEMP_DIR="./temp_download"

# 显示帮助信息
show_help() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -u, --url URL        远端 zip 文件的 URL (默认: $DEFAULT_ZIP_URL)"
    echo "  -t, --temp-dir DIR    临时下载和解压目录 (默认: $DEFAULT_TEMP_DIR)"
    echo "  -h, --help           显示帮助信息"
    echo ""
    echo "示例:"
    echo "  $0 --url https://example.com/aime-skillhub-cli.zip"
}

# 解析命令行参数
URL="$DEFAULT_ZIP_URL"
TEMP_DIR="$DEFAULT_TEMP_DIR"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -u|--url)
            URL="$2"
            shift 2
            ;;
        -t|--temp-dir)
            TEMP_DIR="$2"
            shift 2
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "Error: unknown argument: $1" >&2
            show_help
            exit 1
            ;;
    esac
done

# 检查必要的命令
check_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Error: $1 is required but not installed." >&2
        exit 1
    fi
}

check_command curl
check_command unzip

# 创建临时目录
mkdir -p "$TEMP_DIR"

echo "Downloading zip file from: $URL"
echo "Saving to: $TEMP_DIR/aime-skillhub-cli.zip"

# 下载 zip 文件
if [[ "$URL" == file://* ]]; then
    # 处理本地文件
    local_file="${URL#file://}"
    echo "Copying local file: $local_file"
    cp "$local_file" "$TEMP_DIR/aime-skillhub-cli.zip"
else
    # 处理远程 URL
    echo "Downloading from remote URL: $URL"
    curl -L -o "$TEMP_DIR/aime-skillhub-cli.zip" "$URL"
fi

# 检查下载是否成功
if [[ ! -f "$TEMP_DIR/aime-skillhub-cli.zip" ]]; then
    echo "Error: Failed to download zip file." >&2
    exit 1
fi

echo "Download completed successfully."

# 解压 zip 文件
echo "Extracting zip file..."
unzip -q -o "$TEMP_DIR/aime-skillhub-cli.zip" -d "$TEMP_DIR"

# 查找 aime-install.sh 文件
INSTALL_SCRIPT=""

# 先检查临时目录根目录
if [[ -f "$TEMP_DIR/aime-install.sh" ]]; then
    INSTALL_SCRIPT="$TEMP_DIR/aime-install.sh"
else
    # 再检查子目录
    for dir in "$TEMP_DIR"/*/; do
        if [[ -f "$dir/aime-install.sh" ]]; then
            INSTALL_SCRIPT="$dir/aime-install.sh"
            break
        fi
    done
fi

if [[ -z "$INSTALL_SCRIPT" ]]; then
    echo "Error: aime-install.sh not found in extracted directory." >&2
    exit 1
fi

echo "Found aime-install.sh at: $INSTALL_SCRIPT"

# 执行安装脚本
echo "Executing aime-install.sh..."
chmod +x "$INSTALL_SCRIPT"
"$INSTALL_SCRIPT"

echo "Installation completed successfully."

# 清理临时目录
echo "Cleaning up temporary directory..."
rm -rf "$TEMP_DIR"

echo "Script completed."
