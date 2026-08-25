#!/bin/bash
cd "$(dirname "$0")"
echo "This is a LiveKit Agents plugin, not a standalone server — see README.md."
echo
echo "Checking for a running Moshi server at ws://localhost:8998/api/chat ..."
python3 scripts/check_moshi_server.py
