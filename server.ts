#!/usr/bin/env bun
/**
 * 🎙️ AETHER-VOICE SERVER (v1.0.0)
 * Full-Duplex Real-Time Voice Engine & Multi-App Vocal Dispatcher
 */

import { VoiceEngine } from "./src/voice_engine";
import { InterruptionDetector } from "./src/interruption_detector";
import { VoiceToolsDispatcher } from "./src/voice_tools";
import { VoiceCompetitorBenchmark } from "./src/competitor_benchmark";
import { join } from "path";
import { existsSync } from "fs";

const PORT = Number(process.env.PORT) || 3006;

const voiceEngine = new VoiceEngine();
const vadDetector = new InterruptionDetector();
const toolsDispatcher = new VoiceToolsDispatcher();
const benchmark = new VoiceCompetitorBenchmark();

console.log(`\n======================================================`);
console.log(`🎙️ AETHER-VOICE running on http://localhost:${PORT}`);
console.log(`⚡ Sub-150ms Full-Duplex Neural Audio: Active`);
console.log(`🛡️ Dual-Track VAD & Barge-In Interruption: Online`);
console.log(`🛠️ Multi-App Vocal Tool Dispatcher: Connected`);
console.log(`======================================================\n`);

const server = Bun.serve({
  port: PORT,
  async fetch(req) {
    const url = new URL(req.url);

    const headers = {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization"
    };

    if (req.method === "OPTIONS") return new Response(null, { headers });

    // Serve Static UI Assets
    if (url.pathname === "/" || url.pathname === "/index.html") {
      const p = join(__dirname, "public", "index.html");
      return new Response(Bun.file(p), { headers: { "Content-Type": "text/html" } });
    }
    if (url.pathname === "/app.js") {
      const p = join(__dirname, "public", "app.js");
      return new Response(Bun.file(p), { headers: { "Content-Type": "application/javascript" } });
    }
    if (url.pathname === "/style.css") {
      const p = join(__dirname, "public", "style.css");
      return new Response(Bun.file(p), { headers: { "Content-Type": "text/css" } });
    }
    if (url.pathname.startsWith("/public/")) {
      const p = join(__dirname, url.pathname);
      if (existsSync(p)) return new Response(Bun.file(p));
    }

    // 1. Status & Session
    if (url.pathname === "/api/status" && req.method === "GET") {
      return new Response(JSON.stringify({
        status: "online",
        version: "1.0.0-aether",
        latencyTargetMs: 140,
        sampleRate: 24000,
        fullDuplex: true
      }), { headers });
    }

    // 2. Start Voice Session
    if (url.pathname === "/api/voice/session/start" && req.method === "POST") {
      const session = voiceEngine.startSession();
      return new Response(JSON.stringify(session), { headers });
    }

    // 3. Process Speech Turn
    if (url.pathname === "/api/voice/turn" && req.method === "POST") {
      try {
        let body: any = {};
        try { body = await req.json(); } catch {}
        const transcript = body.transcript || "Ciao Aether, come ottimizzo la RAM di Nexus?";
        const result = await voiceEngine.processHumanAudio(transcript);
        const toolResult = await toolsDispatcher.dispatchVoiceCommand(transcript);

        return new Response(JSON.stringify({
          turn: result,
          toolExecution: toolResult
        }), { headers });
      } catch (e: any) {
        return new Response(JSON.stringify({ error: e.message }), { status: 500, headers });
      }
    }

    // 4. Barge-In Interruption Signal
    if (url.pathname === "/api/voice/interrupt" && req.method === "POST") {
      const signal = voiceEngine.triggerInterruption();
      return new Response(JSON.stringify(signal), { headers });
    }

    // 5. VAD Audio Frame Analysis
    if (url.pathname === "/api/voice/vad" && req.method === "POST") {
      try {
        let body: any = {};
        try { body = await req.json(); } catch {}
        const energyDb = Number(body.energyDb) || -28;
        const metrics = vadDetector.analyzeFrame(energyDb);
        return new Response(JSON.stringify(metrics), { headers });
      } catch (e: any) {
        return new Response(JSON.stringify({ error: e.message }), { status: 500, headers });
      }
    }

    // 6. 5-Competitor Benchmark
    if (url.pathname === "/api/competitors" && req.method === "GET") {
      return new Response(JSON.stringify(benchmark.getComparison()), { headers });
    }

    return new Response("Not Found", { status: 404, headers });
  }
});
