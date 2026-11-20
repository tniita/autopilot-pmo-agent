# GPT バックエンドを持つ PMO Autopilot

## 利用者から見た動作

Claude 版と GPT 版はいずれも Agent 365 のグループ型 Autopilot として公開する。Teams のメンション、差戻し、依頼者本人による承認、Blob の承認記録はモデルに依存しない。同じ Foundry プロジェクトに両方を公開する場合は別のエージェント名・表示名・ブループリントを使い、それぞれ採用する。

## 生成処理と権限

`pmo-autopilot create` に `--provider claude|gpt` を追加し、未指定は従来どおり Claude とする。`--model` は選択したプロバイダーの Foundry デプロイ名とする。GPT 版は OpenAI Python SDK と Foundry の OpenAI 互換 API を使い、推論は実行 ID で認証する。GPT を選んでも Activity プロトコル、Bot Service 認証、Autopilot 公開設定は変更しない。

MCP はアプリが採用インスタンスのエージェントユーザーのトークンで呼び出す。GPT には許可済みツールの定義と実行結果だけを渡し、Bearer トークンは渡さない。実行前に許可リストと Azure DevOps のプロジェクト指定を確認する。既存のターン数・タイムアウト制限を適用し、最終結果は両版とも同じ `Report` と `Evidence.validate` で検証してから保存する。認証失敗・不正な出力で別モデルに自動切替しない。

## モデル別デプロイと検証

Bicep は Anthropic と OpenAI のデプロイを区別し、GPT のみの場合は Claude の組織情報や Marketplace 手続きを要求しない。README には各版のパラメーター、推論ロールのスコープ、デプロイ名、エージェント作成・公開例を示し、共通の Autopilot 採用・デモ手順につなぐ。

ローカルでは両プロバイダーの CLI dry-run、GPT の模擬ツール呼び出しと権限制御、構造・根拠の検証、既存テスト、Bicep コンパイルで確認する。実モデルの提供リージョン・容量、Azure へのデプロイ、実テナントの認証と Teams での通し試験はローカル検証の対象外とする。