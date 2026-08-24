#!/bin/bash
cd "$(dirname "$0")"
echo "🎙️ Starting Aether-Voice on http://localhost:3006..."
bun server.ts
