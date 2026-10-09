#!/usr/bin/env bash
# RPGBar 服务器一键启动（Linux / macOS）
cd "$(dirname "$0")"
exec python3 -m server.main
