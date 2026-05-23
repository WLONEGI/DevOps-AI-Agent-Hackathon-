# Personal Context Engine (Smart Glasses AI Agent) 仕様書

## 1. プロジェクト概要 (Project Overview)
- **名称**: Personal Context Engine (仮称)
- **コンセプト**: 現実世界を正しくキャプチャし、その場その瞬間の「現在の画像1枚」と「音声指示」に対して、汎用LLMを超えた低レイテンシで的確な応答を提供する対話AIエージェント。
- **ターゲットデバイス**: Meta「Ray-Ban Meta スマートグラス」
  - 内蔵された高画質カメラ、マイク、オープンイヤー型ステレオスピーカーをフル活用する。
- **MVP（ハッカソン版）**: **スマートグラス実機 ＋ スマートフォン（iOSエッジゲートウェイ）** の構成で実装する。
  - **Meta Wearables Device Access Toolkit (DAT)** を用いて、グラスからスマートフォンへリアルタイムの映像および音声ストリームをBluetooth経由で伝送する。
  - スマートフォンはゲートウェイとして機能し、WebSocketを用いてクラウド（FastAPI経由でGemini Live API）へ中継する。

---

## 2. アーキテクチャの基本方針 (Architecture Strategy)
開発・検証時のインフラコストを最小化しつつ、最新トレンドを踏まえてネイティブデバイスのポテンシャルを最大限に引き出すアーキテクチャを採用する。

1. **iOSネイティブ (Swift) エッジゲートウェイの採用**: 
   Ray-Ban Metaスマートグラスを iOS アプリに接続する。iOS アプリは Swift で開発し、**Meta Wearables Device Access SDK for iOS** を用いてグラスの映像・音声を取得する。バックグラウンド実行プロセス（`audio` および `bluetooth-central` モード）を維持し、**WebSocket (URLSessionWebSocketTask)** を通じてクラウドへ低遅延な中継ストリーミングを行う。
2. **Gemini Live API への WebSocket 直接連携による中継プロキシ**:
   中継サーバー（FastAPI）を介し、**Gemini Live API (WebSocket)** と常時接続して双方向のリアルタイム音声・映像対話（S2S: Speech-to-Speech）を実現する。
3. **最新の Live API ベストプラクティス適合**:
   - **音声送信仕様**: 音声データは **16kHz PCM** 形式、**20ms〜40ms** のチャンクサイズで送信することで、遅延を最小化する。
   - **音声割り込み対応**: ユーザーの割り込み発話を検知した際に、バックエンドから送信される割り込み信号 (`type: interrupt`) を受け取り、即座にクライアント側の再生バッファを破棄して音声を停止する。
   - **長寿命セッションとコスト管理**: **コンテキストウィンドウ圧縮**（25,000トークンでトリガー、8,000トークンへ削減）により、長時間の対話でもコンテキスト破綻やコスト増大を防ぐ。
   - **レジリエンス（再接続）**: ネットワーク切断に備え、`session_resumption` に対応。サーバーから送信される `resumption_token` を保存しておき、再接続時に引き渡すことでコンテキストを維持したまま会話を復元可能とする。
4. **最新の Gemini Live API モデルの採用**:
   * コアAIモデルには、最新の Live API 対応モデル（デフォルト: `gemini-3.1-flash-live-preview` 等）を採用する。
5. **トリガー契機によるセッション開始**:
   起点はスマートグラス側での **「物理タップ操作」** または特定の **「音声コマンド（ウェイクワード）」** をトリガー（契機）とし、シームレスに中継WebSocket接続を立ち上げる。
6. **音声出力 of 物理的統合**:
   スマートグラスの内蔵ステレオスピーカーを直接利用し、デバイス単体で完結する「Audio-First」かつパーソナルな音声対話環境を提供する。

---

## 2.5 役割分担表 (Role & Responsibility)
システム全体を構成する主要な3層（スマートグラス、iOS、クラウド）の役割・責務を以下に定義する。

| コンポーネント | 役割・責務 |
| :--- | :--- |
| **スマートグラス (Ray-Ban Meta)** | ・物理タップ操作または音声コマンドの検出（セッション開始トリガー）<br>・ユーザーの視界のカメラ撮影（720p 画像ストリーム）<br>・マイクによる音声キャプチャ<br>・内蔵スピーカーによる合成音声の物理的出力（Audio-First） |
| **iOS (エッジゲートウェイ)** | ・**Meta Wearables Device Access SDK** を用いたグラスとのBluetooth通信制御<br>・トリガー検出時のセッション初期化と WebSocket 接続の確立<br>・音声データを **16kHz PCM** にリサンプリングし、**20ms〜40ms** のチャンクで WebSocket 送信<br>・割り込み信号受信時に、即座にスピーカー再生バッファをクリア<br>・`resumption_token` の管理による自動再接続制御<br>・バックグラウンド実行プロセスおよび通信セッションの維持 |
| **クラウド (FastAPI / GCP)** | ・**FastAPI Server (Python)**: iOS端末と Gemini Live API の WebSocket 中継。<br>・各通信中継時におけるミリ秒単位のレイテンシ監視ログの出力。<br>・コンテキストウィンドウ圧縮設定およびセッション再接続ハンドルの仲介。 |

---

## 3. システム構成 (System Architecture)

### 3.1 フロントエンド層 (スマートグラス ＋ iOSエッジゲートウェイアプリ)
- **ハードウェア**: Ray-Ban Meta スマートグラス
- **アプリ技術**: Swift (Native iOS), Meta Wearables Device Access SDK for iOS, WebSocket (Swift Native)
- **機能**: 
  - トリガー検出: 物理タップまたはマイク入力から音声コマンドを検出し、中継セッションを開始。
  - グラス側: 常時カメラによる映像（JPEG）とマイク音声（PCM）をキャプチャ。
  - スマホ側: Bluetoothで受信した映像データをBase64エンコードし、音声データは16kHz PCMにリサンプリングして20-40msチャンクでWebSocket送信。
  - スマホ側: バックエンドから受信した音声（PCM）をグラスのスピーカーへBluetooth転送。割り込み信号検知時は即座に再生バッファをクリア。
  - バックグラウンド実行: iPhoneがロック状態でもバックグラウンドでマイク入力・音声出力を維持する。

### 3.2 バックエンド層 (FastAPI / GCP)
- **技術**: FastAPI / Python / Google GenAI SDK
- **インフラ**: Python Agent 実行環境 (ローカルPC または GCP Free Tier VM)。
- **WebSocketエンドポイント**: `/api/chat` が接続の中継（プロキシ）として機能し、ユーザーの「現在の画像」と「マイク音声」に対して Gemini Live API からの低遅延な音声/テキスト応答をリアルタイムに返送します。

---

## 4. 開発ロードマップ (MVP Milestone)

### Phase 1: ハードウェア連携とインフラ構築
- Meta Wearables Device Access SDK for iOS を使用した、Ray-Ban MetaスマートグラスとiOSアプリ間のBluetooth接続テスト。
- iOSアプリからWebSocketを通じて、グラスからの映像および音声を直接FastAPIサーバー経由でGemini Live APIへ送信するテスト。

### Phase 2: Live API リアルタイム双方向接続の確立
- FastAPIサーバーでの WebSocket プロキシの実装。
- クエリパラメータを用いた動的設定（モデル、音声タイプ）と、ミリ秒単位のレイテンシロギングの導入。

### Phase 3: ベストプラクティス対策の実装
- 音声割り込みシグナルによるクライアント側バッファクリアの統合。
- コンテキストウィンドウの圧縮設定（ContextWindowCompressionConfig）の構成。
- セッション再接続（Session Resumption）トークンの管理・受け渡し。
- iOSエッジゲートウェイ側での 16kHz PCM リサンプリングと 20-40ms の送信チャンク最適化。
