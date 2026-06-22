# iOS Gateway Background Robustness & UI Simplification Plan

This plan details the implementation steps to refine the iOS companion gateway app based on industry-standard background execution best practices and a simplified "settings-only" dashboard UI.

---

## User Review Required

> [!IMPORTANT]
> **Key Decisions to Approve:**
> 1. **BLE State Restoration**: We will configure `CBCentralManager` (under mock/real setup) to support `CBConnectPeripheralOptionRestoreIdentifierKey` so the OS can wake the app up in the background upon smart glasses activity.
> 2. **Auto-Reconnect**: If a disconnect event occurs, the app will automatically enter background scanning/connecting states until the peripheral is retrieved.
> 3. **Collapsible Advanced Settings UI**: We will hide the URL fields, connector mode segment, video preview, and logs within a collapsible "Developer Settings" panel, leaving only the status rings and the simulated trigger button.

---

## Proposed Changes

### iOS Component

#### [MODIFY] [GlassesConnector.swift](file:///Users/negishiyuki/Developments/DevOps-AI-Agent-Hackathon-smart-glass/ios/SmartGlassesGateway/SmartGlassesGateway/Services/GlassesConnector.swift)
* Update Bluetooth initialization/scanning stubs to support CoreBluetooth state restoration.
* Implement auto-reconnect logic: when `centralManager(_:didDisconnectPeripheral:error:)` is called, initiate reconnection immediately.

#### [MODIFY] [AppViewModel.swift](file:///Users/negishiyuki/Developments/DevOps-AI-Agent-Hackathon-smart-glass/ios/SmartGlassesGateway/SmartGlassesGateway/ViewModels/AppViewModel.swift)
* Integrate auto-reconnect status updates and logger messages for restoration events.

#### [MODIFY] [AppView.swift](file:///Users/negishiyuki/Developments/DevOps-AI-Agent-Hackathon-smart-glass/ios/SmartGlassesGateway/SmartGlassesGateway/Views/AppView.swift)
* Redesign UI layout into a clean main dashboard card (Status Rings: Glasses + Gemini; Simulated Action Button).
* Enclose Advanced Settings (Backend URL, Mode selector), Video Feed, and System Console logs into a collapsible disclosure group (`DisclosureGroup` in SwiftUI) labeled "Advanced / Developer Settings".

---

## Verification Plan

### Automated Tests
Run quality verification suite:
```bash
bash scripts/verify.sh
```

### Manual Verification
1. Open the simplified UI in Xcode simulator/device.
2. Verify that Advanced Settings are hidden by default and expand smoothly when clicked.
3. Test simulation action trigger with settings hidden.
4. Verify compiling succeeds on the Xcode simulator.
