# Latency Benchmark Results

## Test Configurations
- **Gemini API Endpoint**: Google AI Studio (API Key Auth)
- **GCP Project (TTS)**: `devops-ai-agent-hackathon`
- **Input Query**: "こんにちは。今日の天気はどうですか？" (~3.3s audio duration)
- **Pipeline A (Live API)**:
  - Model: `gemini-3.1-flash-live-preview`
  - Input: Raw PCM streamed in 32ms chunks (~3.3s sending duration)
  - Output: Native Audio Output (streamed WebSocket chunks)
- **Pipeline B (Sequential STT + LLM + TTS)**:
  - Text Model: `gemini-3.5-flash` (Handles STT via native audio input)
  - Text Output: Streamed text until first sentence delimiter
  - TTS Model: Google Cloud Text-to-Speech (`ja-JP-Standard-A`)
  - Output: Synthesized 24kHz PCM audio

## Latency Summary Table

| Metric (ms) | Pipeline A (Live API) | Pipeline B (STT + Gemini + TTS) | Difference (B - A) |
| :--- | :--- | :--- | :--- |
| **First Audio Frame Latency (Avg)** | **1386.9 ms** | **4128.7 ms** | **2741.9 ms** |
| Min Latency | 1361.7 ms | 3308.4 ms | - |
| Max Latency | 1411.1 ms | 4635.4 ms | - |
| **Total Turn Latency (Avg)** | **5304.0 ms** | **4128.7 ms** | **-1175.3 ms** |

> [!NOTE]
> - **First Audio Frame Latency** represents the time elapsed from the **end of user speech** until the first audio packet is received.
> - **Total Turn Latency** represents the time from the **start of user speech** until the first audio packet is received (including the speech upload duration). For Pipeline B, since the entire audio file is uploaded at once at the start, these two metrics are identical.

## Pipeline B Latency Breakdown (Avg)
- **Gemini Time to First Token**: 3752.8 ms
- **Gemini Time to First Sentence**: 3752.9 ms
- **TTS Synthesis Time (First Sentence)**: 374.9 ms
- **Combined Pipeline B Total Latency**: 4128.7 ms

## Key Findings
1. **Live API Latency Advantage**: The Gemini Live API (`gemini-3.1-flash-live-preview`) has a significant advantage because it processes audio bidirectionally and streams native audio output without waiting for sentence completion or running a separate TTS step.
2. **Sequential Overhead**: Pipeline B requires Gemini to generate the text of the first sentence, wait for punctuation delimiters, transmit the text to the client, call the TTS API, wait for synthesis to complete, and then begin playback. This introduces sequential API overhead.

