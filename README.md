# ⚔️ Quỷ Cốc Bát Hoang — Tu Tiên Discord Bot

> **Thiên Đạo v3.0** — RPG tu tiên nhiều người chơi, chạy hoàn toàn trên Discord.

---

## 📖 Giới Thiệu

Bot Discord RPG thể loại **tu tiên** (cultivation), cho phép người chơi:

- 🧘 Tu luyện, đột phá cảnh giới từ **Luyện Khí** đến **Đăng Tiên**
- ⚔️ Khám phá **Bí Cảnh** với hệ thống combat theo lượt
- 👹 Tham chiến **Boss Thế Giới** cùng toàn server
- 🌿 Thu thập **Linh Căn**, **Thể Chất**, **Pháp Bảo**, **Sủng Thú**
- 🌸 Xây dựng **Quan Hệ**, gia nhập **Tông Môn**
- 🏪 Mua bán qua **Phường Thị** giữa người chơi

---

## 🚀 Tự Deploy

### Yêu Cầu

- Python 3.11+
- PostgreSQL (khuyên dùng [Railway](https://railway.app))
- Discord Bot Token

### 1. Clone & Cài Đặt

```bash
git clone https://github.com/your-username/your-repo.git
cd your-repo
pip install -r requirements.txt
```

### 2. Cấu Hình Environment

Copy file template và điền thông tin thật:

```bash
cp .env.example .env
```

Mở `.env` và điền:

```env
BOT_TOKEN=your_discord_bot_token_here
DATABASE_URL=postgresql://user:pass@host:port/dbname
OWNER_ID=your_discord_user_id_here
```

> **Lấy `OWNER_ID`:** Bật Developer Mode trong Discord → chuột phải vào tên mình → Copy User ID

### 3. Chạy Bot

```bash
python bot.py
```

---

## ☁️ Deploy Lên Railway

### Bước 1 — Tạo Project

1. Đăng nhập [railway.app](https://railway.app)
2. **New Project** → **Deploy from GitHub repo**
3. Chọn repo này

### Bước 2 — Thêm PostgreSQL

1. Trong project → **New** → **Database** → **PostgreSQL**
2. Railway tự tạo biến `DATABASE_URL` — không cần làm gì thêm

### Bước 3 — Thêm Variables

Vào service bot → tab **Variables** → thêm từng biến:

| Variable | Giá trị |
|---|---|
| `BOT_TOKEN` | Token từ [Discord Developer Portal](https://discord.com/developers) |
| `OWNER_ID` | Discord User ID của bạn |
| `BOSS_ANNOUNCE_CHANNEL_ID` | *(tuỳ chọn)* Channel ID để thông báo Boss TG |

> **Lưu ý:** `DATABASE_URL` đã được Railway tự điền khi add PostgreSQL — không cần thêm thủ công.

### Bước 4 — Deploy

Railway tự động deploy sau khi thêm Variables. Bot sẽ online trong ~1 phút.

---

## ⚙️ Cấu Hình Trong Server Discord

Sau khi bot online, dùng các lệnh sau trong server:

```
/setbosschannel channel_id:<ID>   — Chọn channel spawn World Boss
/hoso                              — Bắt đầu tu tiên
```

> **Lấy Channel ID:** Bật Developer Mode → chuột phải vào channel → Copy Channel ID

---

## 📁 Cấu Trúc Project

```
├── bot.py                  # Entry point
├── requirements.txt
├── .env.example            # Template biến môi trường
│
├── cogs/                   # Discord.py Cogs
│   ├── hoso.py             # Lệnh /hoso — gameplay chính
│   ├── hoso_utils.py       # Helpers, stat calc, session
│   ├── combat_task.py      # Auto-combat runner
│   ├── cong_phap.py        # Hệ thống Công Pháp
│   ├── give.py             # Lệnh admin/owner
│   ├── reset.py            # /reset nhân vật
│   ├── thuoc_tinh.py       # /thuoctính xem chỉ số
│   └── views/              # Discord UI Views
│       ├── bi_canh.py      # Bí Cảnh combat
│       ├── boss.py         # World Boss
│       ├── kho_do.py       # Kho đồ & Shop
│       ├── tu_luyen.py     # Tu luyện & Đột phá
│       ├── sung_thu.py     # Sủng Thú
│       ├── quan_he.py      # Quan Hệ & Tặng Quà
│       ├── tong_mon.py     # Tông Môn
│       ├── profile.py      # Chỉnh sửa hồ sơ
│       └── dotpha_tc.py    # Đột Phá Thể Chất
│
└── utils/
    ├── config.py           # Game data & balance (cảnh giới, boss, item...)
    ├── database.py         # PostgreSQL layer (asyncpg)
    ├── bot_emojis.py       # Custom emoji constants
    └── embeds.py           # Discord embed helpers
```

---

## 🎮 Lệnh Slash

| Lệnh | Mô Tả |
|---|---|
| `/hoso` | Mở hồ sơ tu sĩ — toàn bộ gameplay từ đây |
| `/hoso @user` | Xem hồ sơ người khác |
| `/thuoctính` | Xem chi tiết chỉ số chiến đấu |
| `/reset` | Xóa nhân vật và bắt đầu lại (tối đa 3 lần) |
| `/setbosschannel` | *(Admin)* Chọn channel Boss Thế Giới |
| `/spawnboss` | *(Admin)* Force spawn boss để test |
| `/bossstatus` | *(Admin)* Kiểm tra trạng thái boss |
| `/killboss` | *(Owner)* Force kill boss đang active |

---

## 🔒 Bảo Mật

- **Không bao giờ** commit file `.env` lên Git
- `BOT_TOKEN`, `DATABASE_URL`, `OWNER_ID` phải luôn nằm trong Railway Variables hoặc `.env` local
- File `.gitignore` đã được cấu hình để bảo vệ tất cả file nhạy cảm

---

## 📜 License

Private project — không phân phối lại.
