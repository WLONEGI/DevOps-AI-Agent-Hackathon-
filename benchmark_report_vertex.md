# GCP Vertex AI Audio Response Latency Benchmark Results

## Test Configurations
- **GCP Project**: `devops-ai-agent-hackathon`
- **Vertex AI Region**: `us-central1`
- **Input Query**: "こんにちは。今日の天気はどうですか？" (~3.3s audio duration)
- **Pipeline A (GCP Live API)**:
  - Model: `publishers/google/models/gemini-live-2.5-flash-native-audio`
  - Input: Raw PCM streamed in 32ms chunks (~3.3s sending duration)
  - Output: Native Audio Output (streamed WebSocket chunks)
- **Pipeline B (Sequential STT + LLM + TTS)**:
  - Text Model: `publishers/google/models/gemini-2.5-flash` (Handles STT via native audio input)
  - Text Output: Streamed text until first sentence delimiter
  - TTS Model: Google Cloud Text-to-Speech (`ja-JP-Standard-A`)
  - Output: Synthesized 24kHz PCM audio

## Latency Summary Table

| Metric (ms) | Pipeline A (GCP Live API) | Pipeline B (STT + Gemini + TTS) | Difference (B - A) |
| :--- | :--- | :--- | :--- |
| **First Audio Frame Latency (Avg)** | **1101.2 ms** | **6909.9 ms** | **5808.8 ms** |
| Min Latency | 1041.2 ms | 3689.6 ms | - |
| Max Latency | 1212.2 ms | 11331.6 ms | - |
| **Total Turn Latency (Avg)** | **6320.0 ms** | **6909.9 ms** | **589.9 ms** |

> [!NOTE]
> - **First Audio Frame Latency** represents the time elapsed from the **end of user speech** until the first audio packet is received.
> - **Total Turn Latency** represents the time from the **start of user speech** until the first audio packet is received (including the speech upload duration). For Pipeline B, since the entire audio file is uploaded at once at the start, these two metrics are identical.

## Pipeline B Latency Breakdown (Avg)
- **Gemini Time to First Token**: 6163.7 ms
- **Gemini Time to First Sentence**: 6163.7 ms
- **TTS Synthesis Time (First Sentence)**: 744.4 ms
- **Combined Pipeline B Total Latency**: 6909.9 ms

## Key Findings
1. **Live API Latency Advantage**: The GCP Live API (`gemini-live-2.5-flash-native-audio`) has a significant advantage because it processes audio bidirectionally and streams native audio output without waiting for sentence completion or running a separate TTS step.
2. **Sequential Overhead**: Pipeline B requires Gemini to generate the text of the first sentence, wait for punctuation delimiters, transmit the text to the client, call the TTS API, wait for synthesis to complete, and then begin playback. This introduces sequential API overhead.
