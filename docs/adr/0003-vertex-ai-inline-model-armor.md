# 3. Vertex AI Gemini Live APIに対するModel Armorインライン適用のセキュリティ方針

- **ステータス**: Accepted
- **日付**: 2026-06-08
- **起案者**: Antigravity

## 文脈 (Context)

スマートグラス (Ray-Ban Meta) と連携するリアルタイム音声対話AIシステムにおいて、以下の制約と課題が明らかになった：
1. **Live APIの接続制約**: 低遅延の双方向WebSocket音声対話（Speech-to-Speech）を実現する Gemini Live API は、Agent Registryに登録された自律型エージェント（`base_agent: antigravity-preview-05-2026`）のIDを接続先として直接使用できない（仕様上の制限によりエラー `1007` が発生する）。そのため、WebSocketは直接 Vertex AI の基盤モデル（例：`gemini-live-2.5-flash-native-audio`）を呼び出す必要がある。
2. **セキュリティ設計の必要性**: 接続先が基盤モデル直結となるため、ユーザーからの悪意ある入力（プロンプトインジェクション、脱獄等）や、モデルからの不適切な生成出力（有害情報、個人情報の漏洩等）を保護・制御する「ガードレール」を別途構築する必要がある。
3. **パフォーマンスと運用の両立**: 中継プロキシサーバー（FastAPI）側にカスタムのバリデーション処理や他社製のフィルタを都度埋め込むと、コードの肥大化、レイテンシの増加、安全基準の一元管理が難しくなる。

## 決定 (Decision)

Google Cloud が提供するマネージドセキュリティサービスである **Model Armor** を、Vertex AI のインラインセキュリティレイヤー（AIファイアウォール）として全面的に採用・統合する。

### 1. インライン適用（Integrated Services）の設定
* 個別のアプリケーションコードにModel Armor APIの呼び出しを実装するのではなく、プロジェクト全体の **Floor Settings（最小セキュリティ基準）** を設定し、`VERTEX_AI` サービスに対するインライン保護を有効化する。
* これにより、FastAPI中継サーバーを通過して Vertex AI (Gemini Live API) に送受信されるすべてのデータが、インフラストラクチャレベルで自動的に Model Armor を経由して監査・制御される。

### 2. フィルタテンプレートの構成
Model Armorのテンプレートを作成し、以下の項目をフィルタリング・遮断（`INSPECT_AND_BLOCK`）する：
* **インプレス（入力プロンプト）保護**: プロンプトインジェクション、システムプロンプトの窃取攻撃、ジェイルブレイク（脱獄）試行、ハラスメント・危険性の高い入力文。
* **エグレス（出力応答）保護**: クレジットカード番号や個人識別情報 (PII) の意図しない出力（データ漏洩対策）、不適切な音声/テキスト回答の生成。

### 3. 設定の適用方法 (gcloud コマンド)
インフラ構築・変更の際は、以下のGoogle Cloud CLIコマンドを利用してインライン保護を有効化する：
```bash
gcloud model-armor floorsettings update \
  --full-uri=projects/devops-ai-agent-hackathon/locations/global/floorSetting \
  --add-integrated-services=VERTEX_AI \
  --vertex-ai-enforcement-type=INSPECT_AND_BLOCK
```

## 結果 (Consequences)

### メリット:
* **コードの簡素化**: FastAPI中継サーバー（[main.py](file:///Users/negishiyuki/Developments/DevOps-AI-Agent-Hackathon-smart-glass/backend/app/main.py)）側でセキュリティバリデーションロジックを実装・管理する必要がなく、本来の中継機能に特化できる。
* **低遅延の維持**: Google Cloudのインフラレイヤーでインライン処理されるため、アプリ側でREST APIを別途同期的に呼び出す構成に比べて追加レイテンシが極めて少なく、スマートグラスに必要なリアルタイム性が保たれる。
* **ポリシーの一元管理**: セキュリティポリシーの変更や閾値の変更は、GCPコンソールまたはTerraform等の設定更新のみで完了し、コンテナの再デプロイが不要。

### デメリット/トレードオフ:
* **GCPサービス利用料**: インライン Model Armor のスキャン量に応じたGoogle Cloudの利用コストが発生する。
