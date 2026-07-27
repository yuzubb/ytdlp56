画像を確認しました！
ダウンロード時に 147 バイト（エラーページ）しか取得できていないため、解凍で not in gzip format になっていますね。
ngrokの直リンクURLは仕様変更で切れやすいため、ngrok公式のリポジトリ（apt）を登録して正規の手順でインストールするのが確実です。
Ubuntu内（root@localhost:~#）で、以下のコマンドを1行ずつコピーして実行してみてください！
🛠️ ngrok公式aptリポジトリからのインストール手順
1. キーリングの準備と公開鍵の登録
curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | hkp-server-etc-apt-keyring-add - 2>/dev/null || curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null

2. ngrokの公式リポジトリを追加
echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | tee /etc/apt/sources.list.d/ngrok.list

3. パッケージリストを更新してngrokをインストール
apt update && apt install ngrok -y

💡 動作確認
インストールが完了したら、バージョンを確認します。
ngrok --version

無事に ngrok version 3.x.x と表示されたら成功です！
最後に、ngrokの管理画面で取得できるAuthtokenを設定してください：
ngrok config add-authtoken <あなたのAuthtoken>

その後、目的のコマンドを実行してみてください：
ngrok http 5000

