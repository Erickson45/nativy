# Nativy

**AI-powered real-time translation for video calls — built for IT professionals who think better than they speak a foreign language.**

Nativy lets you speak in your native language during a call while the other person hears (and reads) a live translation — and vice-versa. No cloud translation API, no vendor lock-in: the entire AI pipeline (speech-to-text, translation, and voice synthesis) runs locally.

> Built this to practice technical interviews in English without the "thinking in two languages at once" tax. Turned out useful enough to open-source.

---

## How it works

```
 Speaker (pt-BR)                                    Listener (en-US)
      │                                                     ▲
      ▼                                                     │
 ┌─────────────┐    ┌──────────────┐    ┌──────────────┐    │
 │   Audio     │───▶│ Transcription│───▶│ Translation  │────┘
 │  Capture    │    │(faster-whisper)│   │(Ollama/llama3.2)│
 └─────────────┘    └──────────────┘    └──────┬───────┘
                                                │
                                                ▼
                                        ┌──────────────┐
                                        │Voice Synthesis│
                                        │    (gTTS)     │
                                        └──────┬───────┘
                                               │
                                               ▼
                                     Delivered via WebSocket
```

1. **Audio Capture** — the browser records short (~3s) audio chunks via the MediaRecorder API.
2. **Transcription** — `faster-whisper` (base model, CPU) transcribes speech with VAD filtering to skip silence.
3. **Translation** — a local LLM via Ollama (`llama3.2`) translates the text, with a prompt tuned to preserve IT/technical terminology.
4. **Voice Synthesis** — gTTS converts the translated text back into audio, in the chosen accent.
5. **Delivery** — translated audio + text are pushed back to the browser over WebSocket.

The video/audio of the call itself travels **peer-to-peer via WebRTC** (custom signaling) — the backend only ever touches the audio needed for translation, never routes the call media.

## Features

- **Workspace** (`/tradutor`) — solo mode to practice translation in real time.
- **Live Calls** (`/calls`) — two-person WebRTC calls with simultaneous translation overlaid on both sides.
- **History** (`/historico`) — per-user transcription/translation log with audio playback.
- **Voice settings** (`/configuracoes`) — accent and speech-speed options.
- **Auth** (`/login`, `/registrar`) — accounts with saved preferences.

## Tech stack

| Layer | Technology |
|---|---|
| Backend / API | Python, FastAPI |
| Real-time transport | WebSockets, WebRTC (custom signaling) |
| Speech-to-text | faster-whisper |
| Translation (local LLM) | Ollama (llama3.2) |
| Text-to-speech | gTTS |
| Voice activity detection | onnxruntime (optional) |

## Getting started

**Requirements**

- Python 3.11+
- [Ollama](https://ollama.com) with the `llama3.2` model pulled
- FFmpeg
- (optional) `onnxruntime`, for VAD support

```bash
git clone https://github.com/Erickson45/nativy.git
cd nativy
pip install -r requirements.txt

ollama pull llama3.2

uvicorn main:app --reload
```

Then open `http://localhost:8000/tradutor` to try solo mode, or `/calls` to start a two-person call.

## Known limitations

This is a personal project built to explore the problem, not a production system — some honest trade-offs:

- Translation adds a few seconds of latency; it isn't instantaneous.
- No TURN server yet, so calls across strict corporate firewalls/NAT may fail to connect.
- Capped at 2 participants per room.
- gTTS is good enough for practice, not production-grade reliability.
- Sessions are stored in memory — no persistence across restarts.
- Voice synthesis uses accent selection, not true voice cloning.

## Roadmap

- [ ] Low-latency streaming (reduce the transcribe→translate→speak round trip)
- [ ] TURN server for reliable corporate-network connectivity
- [ ] Voice cloning instead of fixed accents
- [ ] Multi-participant rooms (3+)
- [ ] Persistent session storage

## Why this exists

Most "practice your English" tools are flashcards or scripted dialogue. This one is built around a more specific, more honest problem: a technical professional who understands the material perfectly but loses words mid-sentence under the pressure of a live call. Nativy doesn't fix the language gap — it buys you the time and the safety net to think in your own language while still being understood.

## License

MIT
