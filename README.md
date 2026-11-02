# BUFFALO BS-G3024MR DHCP リース情報 Samba AD DNS 自動同期スクリプト

BUFFALO社製 L3スイッチ「**BS-G3024MR**」がDHCPサーバーとして配布しているIPリース情報を、Telnet経由で定期的に自動取得し、**Samba 4 Active Directory Domain Controller (Internal DNS)** のDNSレコード（AレコードおよびPTR逆引きレコード）へ自動登録・更新・削除（同期）を行う運用ツール群です。

---

## 1. システム概要と特徴

```text
+-----------------------+              Telnet (expect)            +--------------------------+
| BUFFALO BS-G3024MR    | <-------------------------------------- | Debian Linux             |
| (DHCP Server)         |                                         | (Samba 4 AD DC)          |
|                       |  show ip dhcp binding                   |                          |
| - VLAN 20: .100-.200  | --------------------------------------> | 1. リース情報取得・正規化   |
| - VLAN 30: .100-.200  |                                         | 2. VLANプール内判定       |
| - VLAN 100: .100-.200 |                                         | 3. ホスト名解決(固定/自動)  |
+-----------------------+                                         | 4. 差分判定 (State管理)   |
                                                                  +--------------------------+
                                                                               |
                                                                      samba-tool dns (A / PTR)
                                                                               v
                                                                  +--------------------------+
                                                                  | Samba Internal DNS       |
                                                                  | - A レコード追加/更新/削除  |
                                                                  | - PTR レコード自動連動     |
                                                                  +--------------------------+
```

### 主な特徴・安全設計
- **BS-G3024MRのTelnet対話仕様への完全対応 (`expect`)**:
  - `login:` / `username:` および `password:` 認証。
  - ログイン直後の curses 形式「Main Menu」画面を検知し、自動で **`n\r`** を送信して CLI（Command Line）へ遷移。
  - プロンプト判定（`BS-G3024MR>` 等の一般モードであれば `enable` で特権モードへ昇格）。
  - 動的ページング制御（`--More--` 検知時の自動スペース送出）による `show ip dhcp binding` の完全取得。
- **配布プール制限による安全性の確保**:
  - VLAN 20 (`192.168.20.100` - `192.168.20.200`)
  - VLAN 30 (`192.168.30.100` - `192.168.30.200`)
  - VLAN 100 (`192.168.100.100` - `192.168.100.200`)
  - ※上記指定範囲外のIP（ゲートウェイ、静的IPサーバー、DC自身など）は**絶対に削除・変更されません**。
- **柔軟なホスト名解決**:
  1. `hosts.map` による MAC/IP 固定ホスト名マッピング（最優先）
  2. NetBIOS クエリ (`nmblookup -A <IP>`) による自動名前解決（オプション）
  3. テンプレートによる自動生成（`dhcp-{mac_clean}`, `dhcp-vlan{vlan}-{last_octet}`, `host-{ip_dashed}` など）
- **状態管理 (State File)**:
  - `/var/lib/buffalo-dhcp-dns/state.json` に前回同期時の登録状態を保持。
  - スイッチから消去されたリース（期限切れ/解放）のみを正確に検知し、DNSから安全に削除（A/PTR）。
- **Samba AD認証方式**:
  - Kerberos keytab 認証 (`-k yes`) または管理者パスワード認証 (`-U Administrator%pass`) に対応。
- **排他制御とドライラン機能**:
  - `flock` による多重実行防止。
  - `--dry-run` オプションで、実DNSへ書き込まずに実行シミュレーションが可能。

---

## 2. ファイル構成

```text
.
├── bin/
│   ├── buffalo_fetch_dhcp.exp    # BS-G3024MR対話制御用 Expectスクリプト
│   └── sync_dhcp_dns.py          # 同期制御・DNS操作・状態管理メインスクリプト
├── config/
│   ├── config.ini.example        # 設定ファイルテンプレート
│   └── hosts.map.example         # MAC/IP 固定ホスト名対応表サンプル
├── systemd/
│   ├── buffalo-dhcp-dns.service  # systemd サービスユニット定義
│   └── buffalo-dhcp-dns.timer    # 定期実行用 systemd タイマー定義（3分間隔）
├── tests/                        # ユニットテスト & モック実機E2E統合テスト
│   ├── mock_buffalo_switch.py
│   ├── test_dns_sync_logic.py
│   ├── test_hostname_resolver.py
│   ├── test_integration_e2e.py
│   ├── test_ip_pools.py
│   └── test_parser.py
├── LICENSE                       # MIT License
└── README.md
```

---

## 3. インストール手順 (Debian Samba AD DC)

### ステップ 1: 前提パッケージのインストール
Debian Linux 側で必要なツール（`expect`, `telnet`, `python3`）をインストールします。

```bash
sudo apt update
sudo apt install -y expect inetutils-telnet python3
```

### ステップ 2: スクリプトの配置と実行権限設定
スクリプトを `/usr/local/bin` へ配置します。

```bash
sudo cp bin/buffalo_fetch_dhcp.exp /usr/local/bin/
sudo cp bin/sync_dhcp_dns.py /usr/local/bin/
sudo chmod +x /usr/local/bin/buffalo_fetch_dhcp.exp
sudo chmod +x /usr/local/bin/sync_dhcp_dns.py
```

### ステップ 3: ディレクトリ作成と設定ファイルの配置
設定用ディレクトリおよび状態保存用ディレクトリを作成します。

```bash
sudo mkdir -p /etc/buffalo-dhcp-dns
sudo mkdir -p /var/lib/buffalo-dhcp-dns
sudo mkdir -p /var/log/buffalo-dhcp-dns

# 設定テンプレートのコピー
sudo cp config/config.ini.example /etc/buffalo-dhcp-dns/config.ini
sudo cp config/hosts.map.example /etc/buffalo-dhcp-dns/hosts.map

# パスワード等が含まれるため権限を 600 (root専用) に設定
sudo chmod 600 /etc/buffalo-dhcp-dns/config.ini
```

### ステップ 4: 設定ファイルの編集
`/etc/buffalo-dhcp-dns/config.ini` を環境に合わせて編集します。

```ini
[switch]
host = 192.168.1.254          # BS-G3024MRの管理IP
port = 23
user = admin                  # スイッチのTelnetユーザー
password = YourSwitchPassword # スイッチのTelnetパスワード
enable_password =             # enableパスワード（不要な場合は空欄）
prompt = BS-G3024MR
timeout = 15
expect_script = /usr/local/bin/buffalo_fetch_dhcp.exp

[samba]
server = 127.0.0.1
domain_zone = corp.example.local    # Active Directory フォワード参照ゾーン
auth_method = kerberos              # 'kerberos' または 'credentials'
user = Administrator
keytab_path = /etc/krb5.keytab      # Kerberos keytab (auth_method=kerberos 時)
sync_ptr = true                     # PTRレコードも同時に管理する場合 true

[reverse_zones]
192.168.20 = 20.168.192.in-addr.arpa
192.168.30 = 30.168.192.in-addr.arpa
192.168.100 = 100.168.192.in-addr.arpa

[vlan_pools]
vlan20 = 192.168.20.100 - 192.168.20.200
vlan30 = 192.168.30.100 - 192.168.30.200
vlan100 = 192.168.100.100 - 192.168.100.200

[hostname]
hosts_map_file = /etc/buffalo-dhcp-dns/hosts.map
enable_netbios = false
template = dhcp-{mac_clean}         # 例: dhcp-001122334401

[storage]
state_file = /var/lib/buffalo-dhcp-dns/state.json
lock_file = /run/lock/buffalo-dhcp-dns.lock
```

### （オプション）Kerberos keytab の作成手順
パスワードを平文で残したくない場合、Samba AD DC 上で以下のコマンドを実行して keytab を生成します。

```bash
sudo samba-tool domain exportkeytab /etc/krb5.keytab --principal=Administrator@CORP.EXAMPLE.LOCAL
sudo chmod 600 /etc/krb5.keytab
```

---

## 4. 動作確認・テスト

### ドライラン（DNSを書き込まずにシミュレーション）
実DNSへの更新を行わずに、スイッチ接続・抽出・差分計算のログを確認できます。

```bash
sudo /usr/local/bin/sync_dhcp_dns.py -c /etc/buffalo-dhcp-dns/config.ini --dry-run -v
```

### サンプル出力ファイルを用いたオフラインテスト
実機に接続せず、出力ファイルから解析と差分計算をテストできます。

```bash
sudo /usr/local/bin/sync_dhcp_dns.py -c /etc/buffalo-dhcp-dns/config.ini -i /tmp/sample.txt --dry-run -v
```

### 現在管理中のリース状態の確認
```bash
sudo /usr/local/bin/sync_dhcp_dns.py -c /etc/buffalo-dhcp-dns/config.ini --list-state
```

---

## 5. systemd による定期自動実行の有効化

`systemd` サービスおよびタイマーを配置して、定期実行（デフォルト: 3分間隔）を有効化します。

```bash
# ユニットファイルの配置
sudo cp systemd/buffalo-dhcp-dns.service /etc/systemd/system/
sudo cp systemd/buffalo-dhcp-dns.timer /etc/systemd/system/

# systemd のリロードとタイマー起動
sudo systemctl daemon-reload
sudo systemctl enable --now buffalo-dhcp-dns.timer

# タイマー状態の確認
sudo systemctl list-timers buffalo-dhcp-dns.timer

# 手動で1回即時実行してログを確認
sudo systemctl start buffalo-dhcp-dns.service
sudo journalctl -u buffalo-dhcp-dns.service -n 50 --no-pager
```

---

## 6. BS-G3024MR Telnet対話の詳細仕様（`expect`）

Expectスクリプト (`bin/buffalo_fetch_dhcp.exp`) は以下の状態遷移を自動処理します：

1. **Telnet接続開始**:
   `spawn telnet $switch_host $switch_port`
2. **ログイン認証**:
   - `(?i)(username|login):` を検知し、ユーザー名を送信
   - `(?i)password:` を検知し、パスワードを送信
3. **curses メインメニューの離脱**:
   - スイッチ初期画面の ANSI/curses メインメニュー表示 (`Main Menu` / `Command Line` 等) を検知
   - **`n\r`** を送信して CLI へ抜ける
4. **プロンプト昇格判定**:
   - `BS-G3024MR>` （一般モード）の場合は `enable\r` を送信
   - （enable パスワードが要求された場合はパスワードを送信）
   - `BS-G3024MR#` （特権モード）へ到達
5. **コマンド実行と動的ページング制御**:
   - `show ip dhcp binding\r` を実行
   - 長文出力により `--More--` 等の停止プロンプトが発生した場合、自動でスペース（` `）を送信して全行をページ送り取得
   - 次の特権プロンプトまでの全出力を変数バッファへ確実に格納
6. **安全な切断**:
   - `exit\r` を送信して Telnet セッションを閉じる
   - バッファを標準出力（stdout）へ渡す

---

## 7. トラブルシューティング

| 現象 | 主な原因 | 対処方法 |
| :--- | :--- | :--- |
| `Timeout waiting for switch response` | スイッチ管理IPへのルーティング不可、またはFW遮断 | `telnet <switch_ip> 23` で手動疎通確認。timeout値を延ばす |
| `Failed to reach privileged prompt` | メニューからCLIへ抜ける文字が異なる、またはプロンプト名が変更されている | スイッチに手動ログインしてプロンプト名を確認し、`config.ini` の `prompt` を修正 |
| `WERR_DNS_ERROR_RECORD_DOES_NOT_EXIST` | 削除対象のレコードがDNS上に元々存在しない | 正常な動作です（警告ログとして記録され、Stateファイルから除去されます） |
| `Kinit failed` または Kerberos エラー | keytab の期限切れ、時刻のズレ、プリンシパル不一致 | `ntpdate` や `chrony` でDCと時刻同期確認。`klist -k /etc/krb5.keytab` で確認 |
| 他プロセスが実行中と表示される | 前回の実行がロックを保持している | `/run/lock/buffalo-dhcp-dns.lock` の存在確認。異常終了時の残骸ならプロセス確認後削除 |

---

## 8. ライセンス

本プロジェクトは [MIT License](LICENSE) の下で公開されています。

Copyright (c) 2026 kiadehi


