# P-Njemin 🚀
Platform peminjaman barang antar pengguna

## Cara Menjalankan

### 1. Install dependencies
```
pip install flask werkzeug
```

### 2. Jalankan aplikasi
```
python app.py
```

### 3. Buka browser
```
http://127.0.0.1:5000
```

## Akun Demo
| Role | Email | Password |
|------|-------|----------|
| Admin | admin@pnjemin.com | admin123 |
| Pemilik | budi@email.com | 123456 |
| Peminjam | siti@email.com | 123456 |

## Admin Panel
```
http://127.0.0.1:5000/admin/login
```

## Halaman yang Tersedia
- `/` — Home (cari & browse barang)
- `/register` — Daftar akun
- `/login` — Masuk
- `/barang/<id>` — Detail barang
- `/booking/<id>` — Form peminjaman
- `/riwayat` — Riwayat transaksi
- `/pembayaran/<id>` — Halaman pembayaran
- `/profil` — Profil & notifikasi
- `/barang_saya` — Kelola barang (pemilik)
- `/upload_barang` — Upload barang baru
- `/admin` — Dashboard admin
- `/admin/users` — Manajemen pengguna
- `/admin/barang` — Manajemen barang
- `/admin/laporan` — Laporan masalah

## Struktur Folder
```
pnjemin/
├── app.py              ← Backend Flask utama
├── database.db         ← Database SQLite (auto-generated)
├── requirements.txt
├── templates/
│   ├── base.html
│   ├── home.html
│   ├── login.html
│   ├── register.html
│   ├── detail_barang.html
│   ├── booking.html
│   ├── riwayat.html
│   ├── pembayaran.html
│   ├── review.html
│   ├── laporan.html
│   ├── profil.html
│   ├── edit_profil.html
│   ├── barang_saya.html
│   ├── upload_barang.html
│   └── admin/
│       ├── base.html
│       ├── login.html
│       ├── dashboard.html
│       ├── users.html
│       ├── barang.html
│       └── laporan.html
└── static/
    └── images/
        └── uploads/    ← Foto upload barang & bukti bayar
```
