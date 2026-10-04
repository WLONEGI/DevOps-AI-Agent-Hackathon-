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
| **First Audio Frame Latency (Avg)** | **1089.5 ms** | **10945.5 ms** | **9856.0 ms** |
| Min Latency | 1023.0 ms | 9041.5 ms | - |
| Max Latency | 1214.4 ms | 13244.5 ms | - |
| **Total Turn Latency (Avg)** | **7328.7 ms** | **10945.5 ms** | **3616.8 ms** |

> [!NOTE]
> - **First Audio Frame Latency** represents the time elapsed from the **end of user speech** until the first audio packet is received.
> - **Total Turn Latency** represents the time from the **start of user speech** until the first audio packet is received (including the speech upload duration). For Pipeline B, since the entire audio file is uploaded at once at the start, these two metrics are identical.

## Pipeline B Latency Breakdown (Avg)
- **Gemini Time to First Token**: 9049.1 ms
- **Gemini Time to First Sentence**: 9049.1 ms
- **TTS Synthesis Time (First Sentence)**: 1892.8 ms
- **Combined Pipeline B Total Latency**: 10945.5 ms

## Key Findings
1. **Live API Latency Advantage**: The GCP Live API (`gemini-live-2.5-flash-native-audio`) has a significant advantage because it processes audio bidirectionally and streams native audio output without waiting for sentence completion or running a separate TTS step.
2. **Sequential Overhead**: Pipeline B requires Gemini to generate the text of the first sentence, wait for punctuation delimiters, transmit the text to the client, call the TTS API, wait for synthesis to complete, and then begin playback. This introduces sequential API overhead.
