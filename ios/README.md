# iOS エッジゲートウェイ (SmartGlassesGateway) セットアップ手順

本ガイドは、Xcode を使用して正規の手順で iOS ゲートウェイアプリを作成し、配布用 Swift ソースファイルをインポートしてビルドするための解説ドキュメントです。

---

## 1. 新規 Xcode プロジェクトの作成

1. Mac で **Xcode** を起動します。
2. **File > New > Project...** を選択します。
3. **iOS > App** を選択し、**Next** をクリックします。
4. プロジェクトの基本情報を入力します：
   - **Product Name**: `SmartGlassesGateway`
   - **Organization Identifier**: `com.example` (任意のID)
   - **Interface**: `SwiftUI`
   - **Language**: `Swift`
5. 保存先を選択し、プロジェクトを作成します。

---

## 2. 外部ライブラリ依存関係の削除について

LiveKit を廃止したため、**外部の WebRTC SDK (LiveKit Swift SDK) は一切不要**です。
Xcode プロジェクト設定の **Package Dependencies** に LiveKit が追加されている場合は、削除して構いません。

---

## 3. バックグラウンド動作と権限の設定 (Info.plist)

スマートグラスからバックグラウンドで音声・映像を受信し、iPhoneがロックされていても通信を維持できるように権限を設定します。

### バックグラウンドモードの有効化
1. Xcode でプロジェクトのアプリターゲットを選択します。
2. **Signing & Capabilities** タブをクリックします。
3. 左上の **「＋ Capability」** ボタンをクリックし、一覧から **Background Modes** を検索して追加します。
4. 追加された **Background Modes** 内で、以下の2つの項目にチェックを入れます：
   - [x] **Audio, AirPlay, and Picture in Picture** (バックグラウンドでのマイク入力/音声再生に必要)
   - [x] **Uses Bluetooth Central accessories** (バックグラウンドでのスマートグラスとの接続維持に必要)

### マイクとBluetoothのアクセス説明文の追加
iOS デバイスの機能へアクセスするためのパーミッションを定義します。

1. 左側のファイルナビゲーターから **Info.plist**（またはターゲット設定の **Info** タブ）を開きます。
2. 以下のキーと説明文（String型）を追加します：

| キー (Key) | 値 (Value) / 説明 |
| :--- | :--- |
| **Privacy - Microphone Usage Description** | スマートグラスのマイク音声を取得し、Gemini Live API経由でAIエージェントに送信するために使用します。 |
| **Privacy - Bluetooth Always Usage Description** | Bluetooth経由でRay-Ban Metaスマートグラスとペアリングし、映像・音声データを受信するために使用します。 |

---

## 4. ソースファイルの追加

以下のディレクトリ内に作成された Swift ファイルを、Xcode のファイルナビゲーターにドラッグ＆ドロップしてプロジェクトにインポートします。

**追加するファイル（計4ファイル）**:
- `ios/SmartGlassesGateway/GlassesConnector.swift` (グラス接続管理)
- `ios/SmartGlassesGateway/GeminiLiveClient.swift` (Gemini WebSocket直接接続と音声・制御のストリーミング)
- `ios/SmartGlassesGateway/AppView.swift` (SwiftUI 画面表示と長期記憶への定期アップロード・Function Calling連携)
- `ios/SmartGlassesGateway/SmartGlassesGatewayApp.swift` (アプリのエントリポイント)

*※インポート時に表示されるダイアログでは、「Copy items if needed」および「Create groups」にチェックが入っていること、ターゲットが「SmartGlassesGateway」に設定されていることを確認してください。*

---

## 5. アプリケーションのエントリポイント変更

`SmartGlassesGatewayApp.swift` を開き、作成した `AppView` を起動するように修正します。

```swift
import SwiftUI

@main
struct SmartGlassesGatewayApp: App {
    var body: some Scene {
        WindowGroup {
            AppView()
        }
    }
}
```

---

## 6. シミュレータと実機での実行方法

- **シミュレータ (Mock Mode)**:
  - Xcode で iPhone シミュレータを選択して実行します。
  - **Gemini API Key** を入力し、ローカルで起動した FastAPI バックエンドの URL（例: `http://localhost:8000`）を設定します。
  - 画面上の「Simulator Mock Mode」トグルをオンにすることで、実機のスマートグラスや Meta SDK なしで、模擬の 1 FPS 映像（テストパターン）とマイク音声入力が使用され、直接 Gemini Live API へ中継されます。
  
- **実機 (Ray-Ban Meta 実機接続)**:
  - 実機 iPhone を Mac に接続し、ターゲットデバイスとして選択してビルドします。
  - バックエンドの URL は Mac のローカル IP（例: `http://192.168.x.x:8000`）に設定します。
  - 実機モードでは、**Meta Wearables Device Access SDK** 経由でペアリング済みの Ray-Ban Meta と接続します。
