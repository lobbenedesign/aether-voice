/**
 * 🎙️ AETHER-VOICE CLIENT SCRIPT
 * Handles Canvas Audio Waveform Rendering, Full-Duplex Speech Turns,
 * Barge-In Interruptions, and Competitor Matrix.
 */

document.addEventListener("DOMContentLoaded", () => {
  setupTabs();
  startAudioVisualizer();
  setupVoiceDialogue();
  fetchCompetitorMatrix();
});

function setupTabs() {
  const tabs = document.querySelectorAll(".nav-tab");
  tabs.forEach(tab => {
    tab.addEventListener("click", () => {
      tabs.forEach(t => t.classList.remove("active"));
      tab.classList.add("active");

      const targetId = `tab-${tab.getAttribute("data-tab")}`;
      document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));
      const targetPane = document.getElementById(targetId);
      if (targetPane) targetPane.classList.add("active");
    });
  });
}

// 1. Audio Visualizer Canvas
let isAudioActive = true;
function startAudioVisualizer() {
  const canvas = document.getElementById("audio-visualizer-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  let phase = 0;
  function render() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const w = canvas.width;
    const h = canvas.height;
    const centerY = h / 2;

    // Draw grid
    ctx.strokeStyle = "rgba(168, 85, 247, 0.08)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, centerY);
    ctx.lineTo(w, centerY);
    ctx.stroke();

    // Draw animated sine/neural audio waveform
    ctx.beginPath();
    ctx.strokeStyle = "#c084fc";
    ctx.lineWidth = 2.5;

    for (let x = 0; x < w; x++) {
      const amp = isAudioActive ? 28 : 4;
      const y = centerY + Math.sin(x * 0.03 + phase) * Math.cos(x * 0.01 + phase) * amp;
      if (x === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // Second cyan harmonic
    ctx.beginPath();
    ctx.strokeStyle = "rgba(56, 189, 248, 0.6)";
    ctx.lineWidth = 1.5;
    for (let x = 0; x < w; x++) {
      const amp = isAudioActive ? 18 : 2;
      const y = centerY + Math.sin(x * 0.05 - phase * 1.5) * amp;
      if (x === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();

    phase += 0.08;
    requestAnimationFrame(render);
  }

  render();
}

// 2. Voice Dialogue & Barge-In
function setupVoiceDialogue() {
  const btnSpeak = document.getElementById("btn-send-speech");
  const btnInterrupt = document.getElementById("btn-interrupt-speech");
  const inputTranscript = document.getElementById("input-voice-transcript");
  const feed = document.getElementById("dialogue-feed-container");
  const dispatchBox = document.getElementById("tool-dispatch-result");

  async function speak() {
    const text = inputTranscript.value.trim();
    if (!text) return;

    // Add human bubble
    appendBubble("human", text);
    btnSpeak.textContent = "🎙️ Streaming Audio...";

    try {
      const res = await fetch("/api/voice/turn", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transcript: text })
      });
      const data = await res.json();

      document.getElementById("chip-latency-target").textContent = `⚡ Latency: ${data.turn.latencyMs} ms`;
      document.getElementById("badge-turn-latency").textContent = `${data.turn.latencyMs} ms Turn-Taking`;

      // Add AI bubble
      appendBubble("ai", data.turn.transcript);

      // Tool dispatch report
      if (data.toolExecution) {
        dispatchBox.innerHTML = `
          <strong style="color: #34d399;">✓ Voice Action Executed:</strong> [${data.toolExecution.targetApp}] ➔ <em>${data.toolExecution.resultSummary}</em>
        `;
      }

      btnSpeak.textContent = "🎙️ Speak";
    } catch (e) {
      btnSpeak.textContent = "🎙️ Speak";
    }
  }

  btnSpeak?.addEventListener("click", speak);

  btnInterrupt?.addEventListener("click", async () => {
    try {
      await fetch("/api/voice/interrupt", { method: "POST" });
      const lastAi = feed.querySelector(".bubble-ai:last-child");
      if (lastAi) {
        lastAi.classList.add("bubble-interrupted");
        lastAi.innerHTML += ` <span style="color: #f43f5e; font-weight: 700;">[🛑 Interrupted by Human]</span>`;
      }
    } catch {}
  });
}

function appendBubble(speaker, text) {
  const feed = document.getElementById("dialogue-feed-container");
  const bubble = document.createElement("div");
  bubble.className = `dialogue-bubble bubble-${speaker}`;
  bubble.innerHTML = `<strong>${speaker === 'human' ? '👤 You' : '🎙️ Aether'}</strong><br>${escapeHtml(text)}`;
  feed.appendChild(bubble);
  feed.scrollTop = feed.scrollHeight;
}

function escapeHtml(text) {
  const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
  return text.replace(/[&<>"']/g, m => map[m]);
}

// 3. Competitors
async function fetchCompetitorMatrix() {
  const container = document.getElementById("competitor-table-container");
  if (!container) return;

  try {
    const res = await fetch("/api/competitors");
    const competitors = await res.json();

    let html = `
      <table class="bench-table">
        <thead>
          <tr>
            <th>Engine / Competitor</th>
            <th>Architecture</th>
            <th>Latency (Turn-Taking)</th>
            <th>Full-Duplex Barge-In</th>
            <th>Local Privacy</th>
            <th>Cost / Minute</th>
            <th>Multi-App Suite Integration</th>
          </tr>
        </thead>
        <tbody>
    `;

    competitors.forEach((c, i) => {
      const isOur = i === 0;
      html += `
        <tr class="${isOur ? 'bench-row-highlight' : ''}">
          <td>${c.name}</td>
          <td>${c.architecture}</td>
          <td style="color: ${c.latencyMs < 200 ? '#34d399' : '#f87171'}; font-weight: 700;">${c.latencyMs} ms</td>
          <td>${c.fullDuplexBargeIn ? '✓ Yes' : '✗ No'}</td>
          <td>${c.localOfflinePrivacy ? '✓ 100% Local' : '☁️ Cloud API'}</td>
          <td>${c.costPerMinute}</td>
          <td>${c.multiAppToolsIntegration ? '✓ Yes' : '✗ No'}</td>
        </tr>
      `;
    });

    html += `</tbody></table>`;
    container.innerHTML = html;
  } catch {}
}
