#!/bin/zsh
# 双击本文件：启动本地服务器并自动打开浏览器。
cd "$(dirname "$0")" || exit 1

PORT=8765
while lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; do
  PORT=$((PORT + 1))
done

URL="http://localhost:$PORT"
echo "单词小侦探已启动：$URL"
echo ""
echo "iPad / 手机（同一 Wi-Fi）可以打开："
for IFACE in en0 en1; do
  IP=$(ipconfig getifaddr "$IFACE" 2>/dev/null)
  [ -n "$IP" ] && echo "  http://$IP:$PORT   （$IFACE）"
done
echo "  也可以试试  http://$(scutil --get LocalHostName 2>/dev/null).local:$PORT"
echo ""
echo "（保持这个窗口开着；按 Ctrl+C 结束）"
(sleep 1; open "$URL") &
exec python3 -m http.server "$PORT"
