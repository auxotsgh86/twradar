#!/bin/bash
cd "$(dirname "$0")"
xattr -dr com.apple.quarantine . 2>/dev/null
bash deploy/install.sh
echo; read -n 1 -s -r -p "按任意鍵關閉視窗"
