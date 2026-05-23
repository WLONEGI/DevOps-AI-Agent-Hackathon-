# 1. LiveKitからGemini Live APIへの直接接続への移行

- **ステータス**: Accepted
- **日付**: 2026-05-23
- **起案者**: Antigravity

## 文脈 (Context)
当初、スマートグラスと AI エージェントの音声・映像ストリーミング接続として LiveKit (WebRTC) を使用していたが、エッジ側（iOS端末）の処理オーバーヘッドと接続アーキテクチャの複雑さが課題であった。また、Gemini Live API が WebSocket を介した低遅延双方向音声ストリーミングを標準サポートするようになった。

## 決定 (Decision)
LiveKit サーバーおよび外部の WebRTC SDK (LiveKit Swift SDK) の依存関係をすべて廃止する。
代わりに、iOS エッジゲートウェイは Gemini Live API (WebSocket) に直接接続し、スマートグラスの音声と制御データをストリーミング中継するアーキテクチャへと移行する。

## 結果 (Consequences)
- 外部インフラ（LiveKit サーバーなど）が不要になり、運用・開発コストが大幅に削減される。
- iOS エッジゲートウェイの CPU・メモリフットプリントが軽減される。
- Gemini Live API に対する WebSocket のやり取りのみに通信仕様が標準化される。
- 今後、インフラや通信に関する方針変更がある場合は、本 ADR を Supersede する新規 ADR を作成する。
