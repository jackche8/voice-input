#!/bin/bash
# Ollama Voice Input — 卸载脚本
# 作者: jackche8  Maintainer: jackche8

DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/install.sh" --uninstall
