# Data Miner auto-farmer

Bot CLI untuk Data Miner Telegram Mini App (`@Datamineer_bot`) di `https://tz.tamimdev.dev/api`.

## Fitur

- Login Telegram lewat Telethon (sekali, session disimpan).
- Mint `initData` segar lewat `messages.requestWebView` tiap start (auto-refresh).
- **Auto-tap tombol mining "DATA"** — tiap tap layar = 0.0005 mined (client-side;
  bot mengakumulasi N tap lalu klaim sekali lewat `/api/user/claim-mining`).
- Loop otomatis:
  - Claim mining balance yang sudah terkumpul.
  - Claim task yang belum selesai.
  - Upgrade level miner kalau saldo cukup.
  - Withdraw ke wallet yang di-lock (opsional).
- Multi akun: satu file JSON per akun di `data/`.

## Cara pakai cepat

```bash
# burst 200 tap sekali (0.0005 x 200 ≈ 0.10 coin), lalu keluar
python bot.py --tap --taps 200

# farm terus-menerus, tiap ronde auto-tap 500x lalu claim otomatis
python bot.py --run --taps 500

# farm tanpa tap, hanya claim mining/task/upgrade pasif
python bot.py --run
```

## Instalasi

```bash
# pakai uv (sudah dipakai di environment ini)
uv venv .venv
uv pip install -r requirements.txt
```

## Setup akun

1. Login Telegram (sekali saja):

   ```bash
   python bot.py --phone +62XXXXXXXXXXX --otp
   ```

   Session tersimpan di `sessions/data-miner.session`.

2. Mint initData (otomatis dipakai oleh `--run`):

   ```bash
   python bot.py --mint
   ```

## Menjalankan

```bash
# satu akun (account.json)
python bot.py --run

# multi akun (data/*.json)
python bot.py --run-all
```

Kode menjaga agar loop tetap jalan kendati ada error sesekali, dan berhenti rapi saat auth betul‐betul gagal (sesi Telegram perlu di‐login ulang).
