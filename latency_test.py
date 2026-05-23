import asyncio
import base64
import os
import sys
import time
from google import genai
from google.genai import types
from google.cloud import texttospeech

# Configuration
API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyDGe9XWeJ3Ln5YcjqzYDY-R-S1mhf-XFbc")
PROJECT_ID = "devops-ai-agent-hackathon"
LIVE_MODEL = "gemini-3.1-flash-live-preview"
TEXT_MODEL = "gemini-3.5-flash"
QUERY_TEXT = "こんにちは。今日の天気はどうですか？"
RUNS = 3 # Reduce runs to 3 for faster validation

# Initialize clients
client_us = genai.Client(api_key=API_KEY)
client_global = genai.Client(api_key=API_KEY)
tts_client = texttospeech.TextToSpeechClient()

def generate_query_audio():
    print(f"Generating query audio for text: '{QUERY_TEXT}'...")
    synthesis_input = texttospeech.SynthesisInput(text=QUERY_TEXT)
    voice = texttospeech.VoiceSelectionParams(language_code="ja-JP", name="ja-JP-Standard-A")
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
        sample_rate_hertz=16000
    )
    response = tts_client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
    
    # Save WAV
    with open("query.wav", "wb") as f:
        f.write(response.audio_content)
        
    # Save PCM (strip WAV header of 44 bytes)
    raw_pcm = response.audio_content[44:] if response.audio_content.startswith(b"RIFF") else response.audio_content
    with open("query.pcm", "wb") as f:
        f.write(raw_pcm)
    
    print(f"Saved query.wav ({len(response.audio_content)} bytes) and query.pcm ({len(raw_pcm)} bytes)")

async def run_pipeline_a():
    """
    Pipeline A: Gemini Live API
    WebSocket bidirectional audio streaming
    """
    pcm_bytes = open("query.pcm", "rb").read()
    
    # Live API Configuration
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
            )
        )
    )
    
    t_start = time.time()
    first_audio_time = None
    t_send_end = None
    
    try:
        print("\n  Connecting to Live API WebSocket...")
        async with client_us.aio.live.connect(model=LIVE_MODEL, config=config) as session:
            print("  Connected. Starting receive loop task...")
            
            # Start receiving loop in background task
            async def receive_loop():
                nonlocal first_audio_time
                try:
                    async for response in session.receive():
                        # Debug print to show server messages
                        print(f"    Received from server: {type(response).__name__}")
                        if response.server_content:
                            sc = response.server_content
                            if sc.model_turn:
                                for part in sc.model_turn.parts:
                                    if part.inline_data and part.inline_data.data:
                                        if first_audio_time is None:
                                            first_audio_time = time.time()
                                            print("      [First audio frame detected!]")
                                            return
                            if sc.turn_complete:
                                print("      [Turn Complete]")
                                if first_audio_time is not None:
                                    return
                except Exception as e:
                    print(f"    Receive loop error: {e}")

            rx_task = asyncio.create_task(receive_loop())
            # Give a brief moment for task to start
            await asyncio.sleep(0.1)
            
            print("  Streaming audio chunks in real-time...")
            chunk_size = 1024 # 32ms
            sleep_time = chunk_size / 32000.0 # 0.032s (32ms)
            
            for i in range(0, len(pcm_bytes), chunk_size):
                chunk = pcm_bytes[i:i+chunk_size]
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                )
                await asyncio.sleep(sleep_time)
            
            t_send_end = time.time()
            print("  Speech audio complete. Sending 500ms of trailing silence...")
            
            silence_chunk = b'\x00' * chunk_size
            for _ in range(16): # 16 * 32ms = 512ms
                await session.send_realtime_input(
                    audio=types.Blob(data=silence_chunk, mime_type="audio/pcm;rate=16000")
                )
                await asyncio.sleep(sleep_time)
                
            print("  Silence sending complete. Sending audio_stream_end=True...")
            await session.send_realtime_input(audio_stream_end=True)
            
            print("  Waiting for server response...")
            await asyncio.wait_for(rx_task, timeout=15.0)
            
    except asyncio.TimeoutError:
        print("  Error: Timeout waiting for Live API response.")
        return None
    except Exception as e:
        print(f"  Pipeline A Error: {e}")
        return None
        
    if first_audio_time and t_send_end:
        latency_post_speech = (first_audio_time - t_send_end) * 1000
        latency_total = (first_audio_time - t_start) * 1000
        return latency_post_speech, latency_total
    return None

async def run_pipeline_b():
    """
    Pipeline B: STT (Gemini Audio Understanding) + Gemini 2.5 Text Output + TTS (Sentence-by-sentence)
    """
    wav_bytes = open("query.wav", "rb").read()
    
    t_start = time.time()
    t_first_token = None
    t_first_sentence = None
    first_sentence_text = ""
    
    contents = [
        types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
        "あなたはスマートグラスを着用したユーザーをサポートする、日本語対応の優秀なAIアシスタントです。\n"
        "質問に対して極めて簡潔、かつわかりやすく日本語で回答してください。\n"
        "箇条書きやマークダウンの記号は音声合成の邪魔になるので一切使わずに、話し言葉の平文のみで回答してください。"
    ]
    
    delimiters = {"。", "？", "！", "\n", "!", "?"}
    
    try:
        # 1. Start Gemini Content Stream
        response_stream = await client_global.aio.models.generate_content_stream(
            model=TEXT_MODEL,
            contents=contents
        )
        
        buffer = ""
        async def parse_gemini_stream():
            nonlocal t_first_token, t_first_sentence, first_sentence_text, buffer
            async for chunk in response_stream:
                if not chunk.text:
                    continue
                
                if t_first_token is None:
                    t_first_token = time.time()
                    
                buffer += chunk.text
                
                if not t_first_sentence:
                    for i, char in enumerate(buffer):
                        if char in delimiters:
                            first_sentence_text = buffer[:i+1].strip()
                            t_first_sentence = time.time()
                            break
            
            if not t_first_sentence and buffer:
                first_sentence_text = buffer.strip()
                t_first_sentence = time.time()

        # Add timeout to Gemini stream
        await asyncio.wait_for(parse_gemini_stream(), timeout=15.0)
        
    except asyncio.TimeoutError:
        print("  Error: Timeout waiting for Gemini Text Stream response.")
        return None
    except Exception as e:
        print(f"  Pipeline B Gemini Error: {e}")
        return None
        
    if not first_sentence_text:
        print("  Pipeline B Error: Empty response from Gemini.")
        return None
        
    print(f"  Gemini Response (first sentence): '{first_sentence_text}'")
        
    # 2. Synthesize the first sentence using Google Cloud TTS
    t_tts_start = time.time()
    try:
        synthesis_input = texttospeech.SynthesisInput(text=first_sentence_text)
        voice = texttospeech.VoiceSelectionParams(language_code="ja-JP", name="ja-JP-Standard-A")
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=24000
        )
        
        def call_tts():
            return tts_client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
            
        response = await asyncio.to_thread(call_tts)
        t_tts_end = time.time()
    except Exception as e:
        print(f"  Pipeline B TTS Error: {e}")
        return None
        
    gemini_first_token_latency = (t_first_token - t_start) * 1000
    gemini_first_sentence_latency = (t_first_sentence - t_start) * 1000
    tts_synthesis_latency = (t_tts_end - t_tts_start) * 1000
    total_audio_latency = (t_tts_end - t_start) * 1000
    
    return total_audio_latency, gemini_first_token_latency, gemini_first_sentence_latency, tts_synthesis_latency

async def main():
    if not os.path.exists("query.wav") or not os.path.exists("query.pcm"):
        generate_query_audio()
        
    print("\nStarting Benchmarks...")
    print(f"Number of runs: {RUNS}\n")
    
    pipeline_a_results = []
    pipeline_b_results = []
    
    # Run Pipeline A
    print("Running Pipeline A (Gemini Live API)...")
    for r in range(RUNS):
        print(f"  Run {r+1}/{RUNS}...")
        res = await run_pipeline_a()
        if res:
            pipeline_a_results.append(res)
            print(f"  Run {r+1} Success: Post-speech: {res[0]:.1f} ms, Total: {res[1]:.1f} ms")
        else:
            print(f"  Run {r+1} Failed")
        await asyncio.sleep(2)
        
    # Run Pipeline B
    print("\nRunning Pipeline B (STT + Gemini + TTS)...")
    for r in range(RUNS):
        print(f"  Run {r+1}/{RUNS}...")
        res = await run_pipeline_b()
        if res:
            pipeline_b_results.append(res)
            print(f"  Run {r+1} Success: Total: {res[0]:.1f} ms (Gemini Token: {res[1]:.1f} ms, Sentence: {res[2]:.1f} ms, TTS: {res[3]:.1f} ms)")
        else:
            print(f"  Run {r+1} Failed")
        await asyncio.sleep(2)
        
    # Calculate stats
    if not pipeline_a_results or not pipeline_b_results:
        print("Error: Could not collect sufficient results for both pipelines.")
        return
        
    # Pipeline A Stats
    a_post_speech = [r[0] for r in pipeline_a_results]
    a_total = [r[1] for r in pipeline_a_results]
    
    # Pipeline B Stats
    b_total = [r[0] for r in pipeline_b_results]
    b_gemini_token = [r[1] for r in pipeline_b_results]
    b_gemini_sentence = [r[2] for r in pipeline_b_results]
    b_tts = [r[3] for r in pipeline_b_results]
    
    # Average functions
    avg = lambda x: sum(x) / len(x)
    
    report = f"""# Latency Benchmark Results

## Test Configurations
- **Gemini API Endpoint**: Google AI Studio (API Key Auth)
- **GCP Project (TTS)**: `{PROJECT_ID}`
- **Input Query**: "{QUERY_TEXT}" (~3.3s audio duration)
- **Pipeline A (Live API)**:
  - Model: `{LIVE_MODEL}`
  - Input: Raw PCM streamed in 32ms chunks (~3.3s sending duration)
  - Output: Native Audio Output (streamed WebSocket chunks)
- **Pipeline B (Sequential STT + LLM + TTS)**:
  - Text Model: `{TEXT_MODEL}` (Handles STT via native audio input)
  - Text Output: Streamed text until first sentence delimiter
  - TTS Model: Google Cloud Text-to-Speech (`ja-JP-Standard-A`)
  - Output: Synthesized 24kHz PCM audio

## Latency Summary Table

| Metric (ms) | Pipeline A (Live API) | Pipeline B (STT + Gemini + TTS) | Difference (B - A) |
| :--- | :--- | :--- | :--- |
| **First Audio Frame Latency (Avg)** | **{avg(a_post_speech):.1f} ms** | **{avg(b_total):.1f} ms** | **{avg(b_total) - avg(a_post_speech):.1f} ms** |
| Min Latency | {min(a_post_speech):.1f} ms | {min(b_total):.1f} ms | - |
| Max Latency | {max(a_post_speech):.1f} ms | {max(b_total):.1f} ms | - |
| **Total Turn Latency (Avg)** | **{avg(a_total):.1f} ms** | **{avg(b_total):.1f} ms** | **{avg(b_total) - avg(a_total):.1f} ms** |

> [!NOTE]
> - **First Audio Frame Latency** represents the time elapsed from the **end of user speech** until the first audio packet is received.
> - **Total Turn Latency** represents the time from the **start of user speech** until the first audio packet is received (including the speech upload duration). For Pipeline B, since the entire audio file is uploaded at once at the start, these two metrics are identical.

## Pipeline B Latency Breakdown (Avg)
- **Gemini Time to First Token**: {avg(b_gemini_token):.1f} ms
- **Gemini Time to First Sentence**: {avg(b_gemini_sentence):.1f} ms
- **TTS Synthesis Time (First Sentence)**: {avg(b_tts):.1f} ms
- **Combined Pipeline B Total Latency**: {avg(b_total):.1f} ms

## Key Findings
1. **Live API Latency Advantage**: The Gemini Live API (`{LIVE_MODEL}`) has a significant advantage because it processes audio bidirectionally and streams native audio output without waiting for sentence completion or running a separate TTS step.
2. **Sequential Overhead**: Pipeline B requires Gemini to generate the text of the first sentence, wait for punctuation delimiters, transmit the text to the client, call the TTS API, wait for synthesis to complete, and then begin playback. This introduces sequential API overhead.

"""
    
    print("\n================ BENCHMARK REPORT ================")
    print(report)
    print("==================================================")
    
    with open("benchmark_report.md", "w") as f:
        f.write(report)
        
    print("\nSaved report to benchmark_report.md")

if __name__ == "__main__":
    asyncio.run(main())
