#!/bin/sh
set -eu
exec /usr/lib/ollama-voice-input/venv/bin/python3 /usr/lib/ollama-voice-input/daemon.py "$@"
