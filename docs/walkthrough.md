# Walkthrough: Direct Model Routing, On-Demand Trigger & Background Robustness

We have successfully refined the iOS gateway app based on background robustness best practices and a simplified settings dashboard. All modifications compile and pass verification checks.

---

## Changes Made

### 1. Deleted Inactivity Silence Timeout
* **[GeminiLiveClient.swift](file:///Users/negishiyuki/Developments/DevOps-AI-Agent-Hackathon-smart-glass/ios/SmartGlassesGateway/SmartGlassesGateway/Services/GeminiLiveClient.swift)**:
  * Deleted all silence detection timers, variables, and callbacks. Sessions now run indefinitely without timeouts until explicitly toggled off by the user.

### 2. Added Action Event Hooking & System Volume Observation on iOS
* **[GlassesConnector.swift](file:///Users/negishiyuki/Developments/DevOps-AI-Agent-Hackathon-smart-glass/ios/SmartGlassesGateway/SmartGlassesGateway/Services/GlassesConnector.swift)**:
  * Added `glassesConnectorDidTriggerAction(_:)` to `GlassesConnectorDelegate`.
  * Added system volume observation KVO (`AVAudioSession.sharedInstance().observe(\.outputVolume)`) to serve as a hardware action button trigger (since smart glasses volume buttons update system volume).
  * Throttled volume triggers to a minimum interval of `1.0s`.

### 3. CoreBluetooth State Restoration & Background Auto-Reconnect
* **[GlassesConnector.swift](file:///Users/negishiyuki/Developments/DevOps-AI-Agent-Hackathon-smart-glass/ios/SmartGlassesGateway/SmartGlassesGateway/Services/GlassesConnector.swift)**:
  * Initialized `CBCentralManager` with a Restoration Identifier (`CBCentralManagerOptionRestoreIdentifierKey`) to enable iOS State Restoration. This wakes up the app in the background if the OS suspends or terminates it while connected.
  * Implemented `attemptAutoReconnect()` using Swift async task sleeping. If the glasses disconnect unexpectedly, the app automatically attempts to reconnect every 3 seconds in the background to restore the Standby state.

### 4. Integrated Action Trigger & State Machine Toggles
* **[AppViewModel.swift](file:///Users/negishiyuki/Developments/DevOps-AI-Agent-Hackathon-smart-glass/ios/SmartGlassesGateway/SmartGlassesGateway/ViewModels/AppViewModel.swift)**:
  * Configured the app to enter a "Standby" state (`glassesConnected == true`, `geminiConnected == false`) when glasses connect.
  * Toggling the action (button or volume KVO) dynamically connects/disconnects the WebSocket and immediately starts/stops the live audio stream.

### 5. Premium UI Refactoring & Collapsible Settings Accordion
* **[AppView.swift](file:///Users/negishiyuki/Developments/DevOps-AI-Agent-Hackathon-smart-glass/ios/SmartGlassesGateway/SmartGlassesGateway/Views/AppView.swift)**:
  * Moved the Backend URL TextField, Mode Segment Picker, Camera Video Feed, and System Logs inside a collapsible `DisclosureGroup` labeled **「詳細・開発者向け設定」** (Advanced / Developer Settings), keeping the main dashboard elegant and focused.

### 6. AVAudioEngine Reordering & Format Safety Guard
* **[GeminiLiveClient.swift](file:///Users/negishiyuki/Developments/DevOps-AI-Agent-Hackathon-smart-glass/ios/SmartGlassesGateway/SmartGlassesGateway/Services/GeminiLiveClient.swift)**:
  * Reordered `setupAudioEngine()` to configure and activate the `AVAudioSession` *before* starting `AVAudioEngine` to prevent instantiating the input tap with a playback-only default format (sample rate 0).
  * Added a safety guard in `startRecording()` to check if the microphone `originalFormat` has a valid `sampleRate > 0` and `channelCount > 0`. If invalid (such as in simulator/permission-denied states), the app logs a warning and returns early, completely preventing CoreAudio tap exceptions from crashing the application.

### 7. Startup and Shutdown Audio Cues on Smart Glasses
* **[GeminiLiveClient.swift](file:///Users/negishiyuki/Developments/DevOps-AI-Agent-Hackathon-smart-glass/ios/SmartGlassesGateway/SmartGlassesGateway/Services/GeminiLiveClient.swift)**:
  * Imported the `AudioToolbox` framework.
  * Added a `playSystemSound(_:)` helper that plays iOS SystemSoundIDs.
  * Played Siri dictation start beep (`1113`) at the beginning of `startAudioStream(currentImage:)`.
  * Played Siri dictation end beep (`1114`) inside `disconnect()` if the session was active (`isConnected` is true) to notify the user of deactivation.
  * Added a `0.15` seconds delay (`DispatchQueue.main.asyncAfter`) before initiating the audio engine microphone tap to prevent the microphone from recording the start beep sound.

### 8. Verification
* Verified quality gate: ran `bash scripts/verify.sh` successfully.

### 9. Fix Audio Playback Termination & Camera CPU Throttling
* **[GeminiLiveClient.swift](file:///Users/negishiyuki/Developments/DevOps-AI-Agent-Hackathon-smart-glass/ios/SmartGlassesGateway/SmartGlassesGateway/Services/GeminiLiveClient.swift)**:
  - Removed `audioEngine.pause()` inside `stopRecording()` to prevent restarting the audio engine and re-activating the `AVAudioSession` during active turns, eliminating hardware/XPC conflicts.
* **[GlassesConnector.swift](file:///Users/negishiyuki/Developments/DevOps-AI-Agent-Hackathon-smart-glass/ios/SmartGlassesGateway/SmartGlassesGateway/Services/GlassesConnector.swift)**:
  - Added a thread-safe `CaptureThrottler` class and its `throttler` instance to perform thread-safe frame rate checks.
  - **iOS Alternative Mode**: Implemented early-return throttling in `captureOutput` before the expensive CIContext CGImage rendering. This reduces the image processing rate from 30 FPS to 5 FPS, cutting CPU/GPU load significantly and preventing XPC connection failures.
  - **Real Device Mode**: Integrated the same `CaptureThrottler` check into `startRealStreaming(session:)` frame receiver block before the CPU-heavy `frame.makeUIImage()` decoding call. This prevents spikes in image decoding overhead during network bursts.

---

## Manual Verification Steps

1. **Simulator / Collapsible UI Verification**:
   * Open the app in the Simulator.
   * Verify the main screen is clean, showing only the header, status rings, and connection buttons.
   * Tap the **「詳細・開発者向け設定」** header. Verify it expands to show the URL settings, mode selection, video frame feed, and log console.
   * Collapsing the panel keeps the app extremely minimal and settings-focused.

2. **Auto-Reconnect & Background Verification**:
   * Pair glasses and start the app.
   * Simulating a disconnect on the glasses' side triggers `GlassesConnector` to print `[GlassesConnector] スマートグラスから切断されました` and immediately attempt auto-reconnection (`[GlassesConnector] バックグラウンド自動再接続を開始します...`).
   * When the device is back in range, the connection is silently restored without user intervention.

3. **Audio Playback Stability & Camera Optimization Verification**:
   * Run the app in `iosAlternative` mode.
   * Trigger the action and talk. Verify that when Gemini responds, recording stops smoothly without restarting the audio engine or causing a `Socket is not connected` error.
   * Verify that audio response playback runs to completion without interruption.
   * Observe CPU/GPU activity and verify that resource consumption remains low because camera frames are throttled to 5 FPS before rendering.
