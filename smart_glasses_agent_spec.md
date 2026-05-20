# Personal Context Engine (Smart Glasses AI Agent) 仕様書

## 1. プロジェクト概要 (Project Overview)
- **名称**: Personal Context Engine (仮称)
- **コンセプト**: 現実世界を正しくキャプチャし、時系列で積み上げることで構築される「その人だけのデータベース」を基盤に動くAIエージェント。汎用LLMには不可能な「パーソナルな文脈（過去の視覚・聴覚情報）」を持ったプロアクティブな音声対話支援を提供する。
- **ターゲットデバイス**: Meta「Ray-Ban Meta スマートグラス」
  - 内蔵された高画質カメラ、マイク、オープンイヤー型ステレオスピーカーをフル活用する。
- **MVP（ハッカソン版）**: **スマートグラス実機 ＋ スマートフォン（iOSエッジゲートウェイ）** の構成で実装する。
  - **Meta Wearables Device Access Toolkit (DAT)** を用いて、グラスからスマートフォンへリアルタイムの映像（720p、1 FPS）および音声ストリームをBluetooth経由で伝送する。
  - スマートフォンはゲートウェイとして機能し、WebRTCを用いてクラウドへ中継する。

---

## 2. アーキテクチャの基本方針 (Architecture Strategy)
開発・検証時のインフラコストを最小化（100%無料枠内）しつつ、最新トレンドを踏まえてネイティブデバイスのポテンシャルを最大限に引き出すアーキテクチャを採用する。

1. **iOSネイティブ (Swift) エッジゲートウェイの採用**: 
   Ray-Ban Metaスマートグラスを iOS アプリに接続する。iOS アプリは Swift で開発し、**Meta Wearables Device Access SDK for iOS** を用いてグラスの映像・音声を取得する。バックグラウンド実行プロセス（`audio` および `bluetooth-central` モード）を維持し、**LiveKit iOS SDK** を通じてクラウドへ低遅延なWebRTCストリーミングを中継する。
2. **LiveKit Cloud (Free Tier) の採用によるサーバーレス運用**:
   独自にWebRTCサーバー（LiveKit Server）をホストする運用・インフラコストを回避するため、マネージドサービスである **LiveKit Cloud の無料枠** を採用する。
   * **無料枠スペック**: 月間50 GBの帯域幅、最大100同時接続分の実行時間。個人開発およびデモ検証には十分なリソースを完全に無料で利用可能。
3. **ローカル ＋ GCP Free Tier で動作する LiveKit Agent (Python)**:
   * 開発・デバッグ時は、開発者PCローカル環境で LiveKit Agent (Python) を実行し、LiveKit Cloud と安全なトークンで接続する。
   * デモのクラウド配置時は、**GCP の常に無料（Always Free）枠である `e2-micro` インスタンス**（米国リージョン）、あるいは従量課金の **Cloud Run** 無料枠内でコンテナを実行する。
4. **AIロジックの GCP Agent Platform 集約と ADK ツール呼出しの初期統合**:
   AIおよびエージェント関連の全機能は **GCP Agent Platform (Agent Development Kit / ADK)** に集約する。初期段階から **ADK ツール呼出し（Function Calling）用インターフェース** をバックエンドに組み込んで設計し、将来的な記憶検索（RAG）や外部API連携へのスムーズな拡張性を担保する。
5. **最新S2S（Speech-to-Speech）および Gemini 3.5 Flash の採用**:
   * コアAIモデルには、最新の **Gemini 3.5 Flash (`gemini-3.5-flash`)** を採用する。Google AI Studio (Free Tier) または Vertex AI Free Tier 経由で、15 RPM の無料枠内で接続する。
   * 従来の「STT → RAG(LLM) → TTS」という直列パイプラインによる高レイテンシを排除するため、**Gemini 3.5 Flash の Multimodal Live API（WebSocketによるリアルタイム双方向音声・映像処理）** を使用する。
   * **サーバーサイドのネイティブVAD（音声活動検知）** を採用し、ユーザーの発話割り込み（Interruption）をサーバー側で検知して自動でエージェントの発話を遮る低遅延な会話制御を実現する。
6. **トリガー契機によるセッション開始**:
   セッションの開始は、スマートグラス側での **「物理タップ操作」** または特定の **「音声コマンド（ウェイクワード）」** をトリガー（契機）とし、シームレスにリアルタイム中継および対話セッションを立ち上げる。
7. **音声出力の物理的統合**:
   Brilliant Labs Frameのような外部イヤホンルーティングを必要とせず、Ray-Ban Metaの内蔵ステレオスピーカーを直接利用する。これにより、デバイス単体で完結する「Audio-First」かつパーソナルな音声対話環境を提供する。

---

## 2.5 役割分担表 (Role & Responsibility)
システム全体を構成する主要な3層（スマートグラス、iOS、クラウド）の役割・責務を以下に定義する。

| コンポーネント | 役割・責務 |
| :--- | :--- |
| **スマートグラス (Ray-Ban Meta)** | ・物理タップ操作または音声コマンドの検出（セッション開始トリガー）<br>・ユーザーの視界のカメラ撮影（720p、1 FPS画像ストリーム）<br>・マイクによる音声キャプチャ<br>・内蔵スピーカーによる合成音声の物理的出力（Audio-First） |
| **iOS (エッジゲートウェイ)** | ・**Meta Wearables Device Access SDK** を用いたグラスとのBluetooth通信制御<br>・トリガー検出時のセッション初期化とWebRTC接続の確立<br>・**LiveKit iOS SDK** を用いた **LiveKit Cloud** への低遅延WebRTC中継（映像・音声トラックのパブリッシュ）<br>・バックグラウンド実行プロセスおよび通信セッションの維持 |
| **クラウド (LiveKit Cloud / GCP)** | ・**LiveKit Cloud (Free Tier)**: メディアストリームのルーティングとセッション管理<br>・**LiveKit Agent (Python)**: ローカルPCまたは GCP Free Tier VM (`e2-micro`) で稼働。映像・音声の購読と Gemini 3.5 Flash Live API への WebSocket 仲介。ADKツールインターフェースのホスト。<br>・**GCP Agent Platform**: Gemini 3.5 Flash による低遅延AI推論、記憶のベクトル保存・検索（RAG）、ツールの自律実行など、AIロジックの完全なオーケストレーション |

---

## 3. システム構成 (System Architecture)

### 3.1 フロントエンド層 (スマートグラス ＋ iOSエッジゲートウェイアプリ)
- **ハードウェア**: Ray-Ban Meta スマートグラス
- **アプリ技術**: Swift (Native iOS), Meta Wearables Device Access SDK for iOS, LiveKit iOS SDK
- **機能**: 
  - トリガー検出: 物理タップまたはマイク入力から音声コマンドを検出し、中継セッションを開始。
  - グラス側: 常時カメラによる映像（720p, 1 FPS）とマイク音声をキャプチャ。
  - スマホ側: Bluetoothで受信した映像・音声データをLiveKitのメディアトラックに変換し、LiveKit CloudへリアルタイムWebRTC送信。
  - スマホ側: バックエンドからの音声ストリーム（S2S）をWebRTCで低遅延受信し、グラスのスピーカーへBluetooth転送。
  - バックグラウンド実行: iPhoneがロック状態でもバックグラウンドでマイク入力・音声出力を維持する。

### 3.2 バックエンド層 (GCP / LiveKit Cloud)
- **技術**: GCP Agent Platform (Agent Development Kit for Python) / LiveKit Agents / Python
- **インフラ**: LiveKit Cloud (WebRTCサーバー) + Python Agent 実行環境 (ローカルPC または GCP Free Tier VM)。

#### パイプラインA：記憶の蓄積（Memory Ingestion）- 非同期処理
1. **フレーム抽出**: LiveKit経由で受信した映像ストリームから、5秒間隔（またはシーン変化検知時）で画像フレーム（キーフレーム）を抽出する。
2. **言語化と情報抽出**: 抽出画像を **Gemini 3.5 Flash** に渡し、詳細なキャプション（状況説明、画面上のテキストOCR、写っているオブジェクトのタグ等）を生成させる。
3. **保存（Memory機能への蓄積）**: GCP Agent Platform のネイティブメモリ/RAG機能に対して、生成されたテキストとタイムスタンプ等のメタデータを直接ベクトル保存する。

#### パイプラインB：記憶の想起と対話【S2S + GCP Agent Platform】
1. **ユーザー入力のリアルタイム処理**: iOSアプリから送信される音声と最新の映像フレームを LiveKit Agents 経由で **Gemini 3.5 Flash Multimodal Live API**（WebSocket接続）にルーティングする。
2. **GCP Agent Platformによるオーケストレーション**:
   システムは **GCP Agent Platform** を頭脳として駆動。エージェントは「ユーザーの意図」を解釈し、過去のコンテキストが必要と判断した場合、ネイティブのメモリ検索ツール（`search_memory`）を自律的に呼び出す。
3. **推論と発話**: エージェントが取得した過去の記憶情報と最新映像を結合して回答音声（PCM）を生成し、iOSアプリ側に音声として直接ストリーミング再生する。これにより、STT/TTSの余分なオーバーヘッドを排除し、対話レイテンシ（T0-T5）を極限まで短縮する。

---

## 4. 記憶データ構造設計 (Memory Design)
**サービス**: GCP Agent Platform (ネイティブ Memory / RAG)
[Rule] 外部DBは利用せず、GCP Agent Platform内で全て完結させる。

| フィールド名 | データ型 | 説明 |
| :--- | :--- | :--- |
| `datapoint_id` | String | 一意のID（UUIDやタイムスタンプを利用） |
| `chunk_text` | String | Gemini 3.5 Flash等が生成したキャプション・コンテキストテキスト（OCRデータ含む） |
| `feature_vector` | Float Array | `chunk_text` の Embedding ベクトル |
| `timestamp` | Timestamp | データ取得時刻（メタデータ検索・フィルタリング用） |
| `location` | String | （オプション）iOSアプリから取得したGPS（緯度・経度）情報 |

---

## 5. ハッカソン用キラーデモ・シナリオ (Killer Demo Scenario)
**テーマ：「伏線回収」型パーソナルアシスタント (Ray-Ban Meta 実機デモ)**

1. **伏線（記憶の蓄積フェーズ）**: 
   デモの最初、ユーザーはPC画面に表示された複雑なソースコードのエラーログや仕様書をグラスの視野（カメラ）に数秒間収める。ユーザーは声を出さず、スマートグラスのカメラが裏側で黙々と映像ストリームを送信し、Ingestion Workerが自動でキーフレームを抽出、Gemini 3.5 Flashにより「仕様書」や「エラーコード」のテキスト情報を言語化して GCP Agent Platform の Memory に蓄積する。
2. **実行（トラブル発生フェーズ）**: 
   数分後、ユーザーが別の作業（コード修正やコマンド実行など）を行っている最中に行き詰まる。ユーザーはグラスのマイクに向かって、「これ、さっきのエラーと関係ある？どう直せばいい？」とだけ曖昧に問いかける。
3. **解決（エージェントの活躍フェーズ）**: 
   エージェントは、現在の視野（最新映像フレーム）と、過去に蓄積された「エラーログ/仕様書の記憶（MemoryからのRAG）」を瞬時に掛け合わせる。
   Gemini 3.5 Flash は「はい、先ほど画面に表示されていたログを確認したところ、型定義が異なっています。現在の行 of 引数を〜のように修正してください」と、**過去の視覚的積み上げがないと絶対に不可能な回答**を、内蔵スピーカーから滑らかな音声で自律的に応答する。

---

## 6. 開発ロードマップ (MVP Milestone)

### Phase 1: ハードウェア連携とインフラ構築 (Day 1)
- Meta Wearables Device Access SDK for iOS を使用した、Ray-Ban MetaスマートグラスとiOSアプリ間のBluetooth接続テスト。
- iOSアプリに LiveKit iOS SDK を組み込み、**LiveKit Cloud (Free Tier)** へグラスからの映像（720p/1fps）および音声を中継するテスト。
- *担当: iOSネイティブアプリエンジニア, インフラエンジニア*

### Phase 2: GCP Agent Platformと記憶パイプラインの構築 (Day 1-2)
- LiveKit Agentを介した映像ストリームの購読と、5秒ごとのキーフレーム抽出処理の実装（ローカル開発環境での動作検証）。
- **Gemini 3.5 Flash** を用いたキーフレームの言語化（OCR/詳細説明）と、**GCP Agent Platformのメモリ機能** を活用したベクトル永続化パイプラインの構築。
- *担当: バックエンド(Agent)エンジニア*

### Phase 3: S2Sと自律エージェント機能の実装 (Day 2-3)
- LiveKit Agents を経由した、iOS端末と Gemini 3.5 Flash Multimodal Live API 間の WebRTC/WebSocket 双方向リアルタイム接続の実装。
- GCP Agent Platform のオーケストレーション機能を活かし、記憶検索ツール（`search_memory`）をエージェントの Function Calling に統合。
- *担当: バックエンド(Agent)エンジニア*

### Phase 4: 品質テスト・レイテンシ最適化 (Day 3)
- iOSアプリのバックグラウンド状態（画面ロック時など）におけるマイク入力と音声再生の安定化。
- エージェントのプロンプト調整、ターン検知（VAD）の感度調整による音声対話レイテンシの極限（1.5秒以下）までの短縮。
- *担当: 全員*
