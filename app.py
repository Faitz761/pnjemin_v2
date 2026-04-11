from flask import Flask, render_template, request, redirect, url_for, session, flash, g
import os
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ── Database: PostgreSQL di production, SQLite di local ──
DATABASE_URL = os.environ.get('DATABASE_URL')

if DATABASE_URL:
    import psycopg2
    import psycopg2.extras
    USE_POSTGRES = True
    # Railway kadang kasih URL dengan prefix 'postgres://', psycopg2 butuh 'postgresql://'
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
else:
    import sqlite3
    USE_POSTGRES = False

# ── Cloudinary: untuk upload foto di production ──
CLOUDINARY_URL = os.environ.get('CLOUDINARY_URL')
USE_CLOUDINARY = bool(CLOUDINARY_URL)
if USE_CLOUDINARY:
    import cloudinary
    import cloudinary.uploader
    cloudinary.config(from_url=CLOUDINARY_URL)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'pnjemin_secret_key_2024')
app.config['UPLOAD_FOLDER'] = 'static/images/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

def allowed_file(f):
    return '.' in f and f.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ── Jinja2 filters: handle datetime object (PostgreSQL) vs string (SQLite) ──
@app.template_filter('tgl')
def filter_tgl(value, fmt='%Y-%m-%d'):
    if not value:
        return ''
    if hasattr(value, 'strftime'):
        return value.strftime(fmt)
    return str(value)[:10]

@app.template_filter('tglwaktu')
def filter_tglwaktu(value):
    if not value:
        return ''
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d %H:%M')
    return str(value)[:16]

@app.template_filter('foto_src')
def filter_foto_src(value):
    """Handle foto: kalau URL lengkap (Cloudinary) pakai langsung, kalau filename pakai static."""
    if not value:
        return ''
    if value.startswith('http://') or value.startswith('https://'):
        return value
    return '/static/images/uploads/' + value

# ══════════════════════════════════════════════════
#  DATABASE HELPERS (support PostgreSQL + SQLite)
# ══════════════════════════════════════════════════

def get_db():
    if 'db' not in g:
        if USE_POSTGRES:
            g.db = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            import sqlite3
            g.db = sqlite3.connect('database.db', check_same_thread=False)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA journal_mode=WAL")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def db_execute(sql, params=(), fetchone=False, fetchall=False, commit=False):
    """Helper universal untuk query. Otomatis handle perbedaan PostgreSQL vs SQLite."""
    conn = get_db()
    # PostgreSQL pakai %s, SQLite pakai ?
    if USE_POSTGRES:
        sql = sql.replace('?', '%s')
        # SQLite pakai AUTOINCREMENT, PostgreSQL pakai SERIAL/IDENTITY (sudah di init_db)
    cur = conn.cursor()
    cur.execute(sql, params)
    result = None
    if fetchone:
        result = cur.fetchone()
    elif fetchall:
        result = cur.fetchall()
    if commit:
        conn.commit()
    cur.close()
    return result

def db_executescript(sql):
    """Untuk init_db: jalankan banyak statement sekaligus."""
    conn = get_db()
    if USE_POSTGRES:
        cur = conn.cursor()
        cur.execute(sql)
        conn.commit()
        cur.close()
    else:
        conn.executescript(sql)
        conn.commit()

# ══════════════════════════════════════════════════
#  APP HELPERS
# ══════════════════════════════════════════════════

def add_notif(id_user, pesan):
    db_execute("INSERT INTO notifikasi (id_user, pesan) VALUES (?,?)", (id_user, pesan), commit=True)

def notif_count():
    if 'user_id' not in session: return 0
    row = db_execute("SELECT COUNT(*) AS c FROM notifikasi WHERE id_user=? AND dibaca=0", (session['user_id'],), fetchone=True)
    return row['c'] if row else 0

def chat_unread():
    if 'user_id' not in session: return 0
    try:
        row = db_execute("SELECT COUNT(*) AS c FROM chat WHERE id_penerima=? AND dibaca=0", (session['user_id'],), fetchone=True)
        return row['c'] if row else 0
    except: return 0

def foto_url(foto):
    """Otomatis detect: kalau foto adalah URL lengkap (Cloudinary), pakai langsung.
    Kalau nama file biasa, pakai path /static/images/uploads/."""
    if not foto:
        return None
    if foto.startswith('http://') or foto.startswith('https://'):
        return foto
    return '/static/images/uploads/' + foto

@app.context_processor
def inject_globals():
    return dict(chat_unread_count=chat_unread(), foto_url=foto_url)

def cek_blokir():
    if 'user_id' in session:
        user = db_execute("SELECT status FROM users WHERE id=?", (session['user_id'],), fetchone=True)
        if user and user['status'] == 'diblokir':
            return True
    return False

def cek_auto_freeze():
    """Bekukan akun peminjam otomatis jika denda tidak dibayar 2x24 jam."""
    if 'user_id' not in session: return
    if session.get('tipe_akun') != 'peminjam': return
    denda_telat = db_execute("""
        SELECT l.id FROM laporan l
        JOIN transaksi t ON l.id_transaksi=t.id
        WHERE t.id_user=? AND l.tipe_pelapor='pemilik'
        AND l.status='selesai' AND l.total_tagihan > 0
        AND l.created_at < NOW() - INTERVAL '48 hours'
    """ if USE_POSTGRES else """
        SELECT l.id FROM laporan l
        JOIN transaksi t ON l.id_transaksi=t.id
        WHERE t.id_user=? AND l.tipe_pelapor='pemilik'
        AND l.status='selesai' AND l.total_tagihan > 0
        AND l.created_at < datetime('now', '-48 hours')
    """, (session['user_id'],), fetchone=True)
    if denda_telat:
        db_execute("UPDATE users SET status='diblokir' WHERE id=?",
                   (session['user_id'],), commit=True)
        session['status'] = 'diblokir'

def save_foto(f, prefix):
    """Upload foto ke Cloudinary (production) atau lokal (development)."""
    if not f or not f.filename or not allowed_file(f.filename):
        return None
    if USE_CLOUDINARY:
        try:
            result = cloudinary.uploader.upload(
                f,
                folder="pnjemin",
                public_id=f"{prefix}_{secure_filename(f.filename).rsplit('.', 1)[0]}",
                overwrite=True
            )
            return result['secure_url']  # Simpan URL lengkap ke DB
        except Exception as e:
            print(f"Cloudinary upload error: {e}")
            return None
    else:
        # Mode lokal / development
        filename = secure_filename(f'{prefix}_{f.filename}')
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        f.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        return filename

# ══════════════════════════════════════════════════
#  INIT DATABASE
# ══════════════════════════════════════════════════

def init_db():
    if USE_POSTGRES:
        sql = '''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                nama TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
                no_hp TEXT, alamat TEXT, role TEXT DEFAULT 'user',
                tipe_akun TEXT DEFAULT 'peminjam', rating REAL DEFAULT 0,
                total_transaksi INTEGER DEFAULT 0, status TEXT DEFAULT 'aktif',
                foto_ktp TEXT, nik TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS barang (
                id SERIAL PRIMARY KEY,
                nama_barang TEXT NOT NULL, deskripsi TEXT, harga_sewa REAL NOT NULL,
                stok INTEGER DEFAULT 1, lokasi TEXT, kategori TEXT, foto TEXT,
                id_pemilik INTEGER, rating REAL DEFAULT 0, total_disewa INTEGER DEFAULT 0,
                status TEXT DEFAULT 'tersedia', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_pemilik) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS transaksi (
                id SERIAL PRIMARY KEY,
                id_user INTEGER, id_barang INTEGER, tanggal_pinjam TEXT,
                durasi INTEGER, metode_pengambilan TEXT, metode_pembayaran TEXT,
                biaya_sewa REAL, total_biaya REAL,
                status TEXT DEFAULT 'menunggu_persetujuan',
                bukti_pembayaran TEXT, catatan TEXT, denda REAL DEFAULT 0,
                foto_serah TEXT, foto_terima TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_user) REFERENCES users(id),
                FOREIGN KEY (id_barang) REFERENCES barang(id)
            );
            CREATE TABLE IF NOT EXISTS review (
                id SERIAL PRIMARY KEY,
                id_transaksi INTEGER, id_reviewer INTEGER, rating INTEGER, komentar TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_transaksi) REFERENCES transaksi(id),
                FOREIGN KEY (id_reviewer) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS laporan (
                id SERIAL PRIMARY KEY,
                id_pelapor INTEGER, id_transaksi INTEGER, jenis_masalah TEXT,
                deskripsi TEXT, foto_bukti TEXT, status TEXT DEFAULT 'menunggu',
                keputusan TEXT, tipe_pelapor TEXT DEFAULT 'peminjam',
                respon_pemilik TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_pelapor) REFERENCES users(id),
                FOREIGN KEY (id_transaksi) REFERENCES transaksi(id)
            );
            CREATE TABLE IF NOT EXISTS notifikasi (
                id SERIAL PRIMARY KEY, id_user INTEGER, pesan TEXT,
                dibaca INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_user) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS banding (
                id SERIAL PRIMARY KEY, id_user INTEGER, alasan TEXT,
                status TEXT DEFAULT 'menunggu', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_user) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS chat (
                id SERIAL PRIMARY KEY,
                id_pengirim INTEGER NOT NULL, id_penerima INTEGER NOT NULL,
                id_barang INTEGER,
                pesan TEXT NOT NULL, dibaca INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_pengirim) REFERENCES users(id),
                FOREIGN KEY (id_penerima) REFERENCES users(id),
                FOREIGN KEY (id_barang) REFERENCES barang(id)
            );
            CREATE TABLE IF NOT EXISTS review_peminjam (
                id SERIAL PRIMARY KEY,
                id_transaksi INTEGER, id_pemilik INTEGER, id_peminjam INTEGER,
                rating INTEGER, komentar TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_transaksi) REFERENCES transaksi(id),
                FOREIGN KEY (id_pemilik) REFERENCES users(id),
                FOREIGN KEY (id_peminjam) REFERENCES users(id)
            );
        '''
    else:
        sql = '''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nama TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
                no_hp TEXT, alamat TEXT, role TEXT DEFAULT 'user',
                tipe_akun TEXT DEFAULT 'peminjam', rating REAL DEFAULT 0,
                total_transaksi INTEGER DEFAULT 0, status TEXT DEFAULT 'aktif',
                foto_ktp TEXT, nik TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS barang (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nama_barang TEXT NOT NULL, deskripsi TEXT, harga_sewa REAL NOT NULL,
                stok INTEGER DEFAULT 1, lokasi TEXT, kategori TEXT, foto TEXT,
                id_pemilik INTEGER, rating REAL DEFAULT 0, total_disewa INTEGER DEFAULT 0,
                status TEXT DEFAULT 'tersedia', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_pemilik) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS transaksi (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_user INTEGER, id_barang INTEGER, tanggal_pinjam TEXT,
                durasi INTEGER, metode_pengambilan TEXT, metode_pembayaran TEXT,
                biaya_sewa REAL, total_biaya REAL,
                status TEXT DEFAULT 'menunggu_persetujuan',
                bukti_pembayaran TEXT, catatan TEXT, denda REAL DEFAULT 0,
                foto_serah TEXT, foto_terima TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_user) REFERENCES users(id),
                FOREIGN KEY (id_barang) REFERENCES barang(id)
            );
            CREATE TABLE IF NOT EXISTS review (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_transaksi INTEGER, id_reviewer INTEGER, rating INTEGER, komentar TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_transaksi) REFERENCES transaksi(id),
                FOREIGN KEY (id_reviewer) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS laporan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pelapor INTEGER, id_transaksi INTEGER, jenis_masalah TEXT,
                deskripsi TEXT, foto_bukti TEXT, status TEXT DEFAULT 'menunggu',
                keputusan TEXT, tipe_pelapor TEXT DEFAULT 'peminjam',
                respon_pemilik TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_pelapor) REFERENCES users(id),
                FOREIGN KEY (id_transaksi) REFERENCES transaksi(id)
            );
            CREATE TABLE IF NOT EXISTS notifikasi (
                id INTEGER PRIMARY KEY AUTOINCREMENT, id_user INTEGER, pesan TEXT,
                dibaca INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_user) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS banding (
                id INTEGER PRIMARY KEY AUTOINCREMENT, id_user INTEGER, alasan TEXT,
                status TEXT DEFAULT 'menunggu', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_user) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS chat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pengirim INTEGER NOT NULL, id_penerima INTEGER NOT NULL,
                id_barang INTEGER,
                pesan TEXT NOT NULL, dibaca INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_pengirim) REFERENCES users(id),
                FOREIGN KEY (id_penerima) REFERENCES users(id),
                FOREIGN KEY (id_barang) REFERENCES barang(id)
            );
            CREATE TABLE IF NOT EXISTS review_peminjam (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_transaksi INTEGER, id_pemilik INTEGER, id_peminjam INTEGER,
                rating INTEGER, komentar TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_transaksi) REFERENCES transaksi(id),
                FOREIGN KEY (id_pemilik) REFERENCES users(id),
                FOREIGN KEY (id_peminjam) REFERENCES users(id)
            );
        '''

    db_executescript(sql)

# ── Migrasi kolom baru ──
    migrasi_transaksi = [
        ('foto_checkin','TEXT'), ('checkin_status','TEXT'),
        ('checkin_catatan','TEXT'), ('checkin_at','TIMESTAMP'),
        ('denda_status','TEXT'), ('denda_due','TIMESTAMP'),
    ]
    migrasi_laporan = [
        ('nominal_denda','REAL'), ('potongan_platform','REAL'),
        ('pemilik_terima','REAL'), ('total_tagihan','REAL'),
        ('kategori_kerusakan','TEXT'), ('status_validasi','TEXT'),
        ('alasan_tolak','TEXT'), ('nominal_pemilik','REAL'),
    ]
    if USE_POSTGRES:
        kolom_baru = [
        ("laporan", "total_tagihan", "REAL"),
        ("laporan", "nominal_pemilik", "REAL"),
        ("laporan", "potongan_platform", "REAL"),
        ("laporan", "kategori_kerusakan", "TEXT"),
        ("laporan", "status_validasi", "TEXT"),
        ("laporan", "alasan_tolak", "TEXT"),
        ("transaksi", "denda_status", "TEXT"),
        ("transaksi", "denda_due", "TIMESTAMP"),
    ]
    for tabel, col, tipe in kolom_baru:
        try:
            db_execute(f"ALTER TABLE {tabel} ADD COLUMN IF NOT EXISTS {col} {tipe}", commit=True)
        except: pass

        for col, defval in migrasi_transaksi:
            try: db_execute(f"ALTER TABLE transaksi ADD COLUMN {col} {defval}", commit=True)
            except: pass
        for col, defval in migrasi_laporan:
            try: db_execute(f"ALTER TABLE laporan ADD COLUMN {col} {defval}", commit=True)
            except: pass
    else:
        conn = get_db()
        c = conn.cursor()
        for col, defval in migrasi_transaksi + migrasi_laporan:
            tabel = 'transaksi' if col in [x for x,_ in migrasi_transaksi] else 'laporan'
            try: c.execute(f"ALTER TABLE {tabel} ADD COLUMN {col} {defval}")
            except: pass
        conn.commit()

# Migrasi: tambah kolom checkin jika belum ada
  #  if USE_POSTGRES:
  #      for col, defval in [('foto_checkin','TEXT'), ('checkin_status','TEXT'), ('checkin_catatan','TEXT'), ('checkin_at','TIMESTAMP')]:
  #          try:
      #          db_execute(f"ALTER TABLE transaksi ADD COLUMN {col} {defval}", commit=True)
     #       except: pass
    #else:
      #  conn = get_db()
      #  c = conn.cursor()
     #   for col, defval in [('foto_checkin','TEXT'), ('checkin_status','TEXT'), ('checkin_catatan','TEXT'), ('checkin_at','TIMESTAMP')]:
    #        try:
   #             c.execute(f"ALTER TABLE transaksi ADD COLUMN {col} {defval}")
  #          except: pass
 #       conn.commit()

#for col, defval in [('nominal_denda','REAL'), ('potongan_platform','REAL'), ('pemilik_terima','REAL')]:
  #  try:
   #     db_execute(f"ALTER TABLE laporan ADD COLUMN {col} {defval}", commit=True) if USE_POSTGRES else None
  #  except: pass
#if not USE_POSTGRES:
   # conn = get_db()
  #  c = conn.cursor()
   # for col, defval in [('nominal_denda','REAL'), ('potongan_platform','REAL'), ('pemilik_terima','REAL')]:
   #     try:
  #          c.execute(f"ALTER TABLE laporan ADD COLUMN {col} {defval}")
  #      except: pass
  #  conn.commit()

    # Buat admin jika belum ada
    existing = db_execute("SELECT id FROM users WHERE email='admin@pnjemin.com'", fetchone=True)
    if not existing:
        db_execute(
            "INSERT INTO users (nama,email,password,role,tipe_akun) VALUES (?,?,?,?,?)",
            ('Admin','admin@pnjemin.com',generate_password_hash('admin123'),'admin','admin'),
            commit=True
        )
    
    

    # Seed data barang demo jika belum ada
    existing_barang = db_execute("SELECT id FROM barang LIMIT 1", fetchone=True)
    if not existing_barang:
        # Buat user demo
        budi = db_execute("SELECT id FROM users WHERE email='budi@email.com'", fetchone=True)
        if not budi:
            db_execute(
                "INSERT INTO users (nama,email,password,no_hp,alamat,tipe_akun) VALUES (?,?,?,?,?,?)",
                ('Budi Santoso','budi@email.com',generate_password_hash('123456'),'081234567890','Jl. Merdeka No.1, Jakarta','pemilik'),
                commit=True
            )
            db_execute(
                "INSERT INTO users (nama,email,password,no_hp,alamat,tipe_akun) VALUES (?,?,?,?,?,?)",
                ('Siti Rahayu','siti@email.com',generate_password_hash('123456'),'089876543210','Jl. Sudirman No.5, Jakarta','peminjam'),
                commit=True
            )
        budi = db_execute("SELECT id FROM users WHERE email='budi@email.com'", fetchone=True)
        pid = budi['id']
        items = [
            ('Tenda Camping 4 Orang','Tenda berkualitas waterproof, cocok untuk camping 4 orang.',75000,3,'Jakarta Selatan','Olahraga & Outdoor',None,pid),
            ('Kamera DSLR Canon 700D','Kamera DSLR lengkap lensa kit 18-55mm dan memory card 32GB.',150000,1,'Jakarta Pusat','Elektronik',None,pid),
            ('Drone DJI Mini 3','Drone aerial photography, baterai 3 unit, remote controller.',200000,1,'Jakarta Barat','Elektronik',None,pid),
            ('Sepeda Gunung MTB','Sepeda gunung full suspension, ukuran 27.5 inch.',100000,2,'Bandung','Olahraga & Outdoor',None,pid),
            ('Proyektor Portable Epson','Proyektor 3000 lumens, resolusi HD, layar portable sudah termasuk.',125000,1,'Jakarta Timur','Elektronik',None,pid),
            ('Alat Snorkeling Set','Set snorkeling lengkap: masker, fin, snorkel, dan tas.',50000,5,'Bali','Olahraga & Outdoor',None,pid),
        ]
        for item in items:
            db_execute(
                "INSERT INTO barang (nama_barang,deskripsi,harga_sewa,stok,lokasi,kategori,foto,id_pemilik) VALUES (?,?,?,?,?,?,?,?)",
                item, commit=True
            )

# ══════════════════════════════════════════════════
#  USER ROUTES
# ══════════════════════════════════════════════════

@app.route('/')
def home():
    search = request.args.get('search','')
    kategori = request.args.get('kategori','')
    if search:
        if USE_POSTGRES:
            barang = db_execute("SELECT b.*,u.nama as nama_pemilik FROM barang b JOIN users u ON b.id_pemilik=u.id WHERE b.nama_barang ILIKE ? AND b.status='tersedia' ORDER BY b.total_disewa DESC",(f'%{search}%',), fetchall=True)
        else:
            barang = db_execute("SELECT b.*,u.nama as nama_pemilik FROM barang b JOIN users u ON b.id_pemilik=u.id WHERE b.nama_barang LIKE ? AND b.status='tersedia' ORDER BY b.total_disewa DESC",(f'%{search}%',), fetchall=True)
    elif kategori:
        barang = db_execute("SELECT b.*,u.nama as nama_pemilik FROM barang b JOIN users u ON b.id_pemilik=u.id WHERE b.kategori=? AND b.status='tersedia' ORDER BY b.total_disewa DESC",(kategori,), fetchall=True)
    else:
        barang = db_execute("SELECT b.*,u.nama as nama_pemilik FROM barang b JOIN users u ON b.id_pemilik=u.id WHERE b.status='tersedia' ORDER BY b.total_disewa DESC", fetchall=True)
    return render_template('home.html', barang=barang, search=search, kategori=kategori, notif_count=notif_count())

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        nama,email,password = request.form['nama'],request.form['email'],request.form['password']
        no_hp,alamat,tipe_akun = request.form['no_hp'],request.form['alamat'],request.form['tipe_akun']
        if db_execute("SELECT id FROM users WHERE email=?",(email,), fetchone=True):
            flash('Email sudah terdaftar!','error')
            return redirect(url_for('register'))
        db_execute("INSERT INTO users (nama,email,password,no_hp,alamat,tipe_akun) VALUES (?,?,?,?,?,?)",
                   (nama,email,generate_password_hash(password),no_hp,alamat,tipe_akun), commit=True)
        flash('Registrasi berhasil! Silakan login.','success')
        return redirect(url_for('login'))
    return render_template('register.html', notif_count=notif_count())

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email,password = request.form['email'],request.form['password']
        user = db_execute("SELECT * FROM users WHERE email=?",(email,), fetchone=True)
        if user and check_password_hash(user['password'], password):
            if user['role'] == 'admin':
                flash('Gunakan halaman login admin!','error')
                return redirect(url_for('admin_login'))
            session.update({'user_id':user['id'],'nama':user['nama'],'tipe_akun':user['tipe_akun'],'role':user['role'],'status':user['status']})
            if user['status'] == 'diblokir':
                return redirect(url_for('akun_diblokir'))
            cek_auto_freeze()
            if session.get('status') == 'diblokir':
                return redirect(url_for('akun_diblokir'))
            return redirect(request.args.get('next') or url_for('home'))
        flash('Email atau password salah!','error')
    return render_template('login.html', notif_count=notif_count())

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

@app.route('/akun_diblokir')
def akun_diblokir():
    if 'user_id' not in session: return redirect(url_for('login'))
    banding_ada = db_execute("SELECT id FROM banding WHERE id_user=? AND status='menunggu'",(session['user_id'],), fetchone=True)
    return render_template('akun_diblokir.html', banding_ada=banding_ada, notif_count=0)

@app.route('/ajukan_banding', methods=['POST'])
def ajukan_banding():
    if 'user_id' not in session: return redirect(url_for('login'))
    alasan = request.form['alasan']
    db_execute("INSERT INTO banding (id_user,alasan) VALUES (?,?)",(session['user_id'],alasan), commit=True)
    flash('Banding berhasil diajukan! Tunggu keputusan admin.','success')
    return redirect(url_for('akun_diblokir'))

@app.route('/barang/<int:id>')
def detail_barang(id):
    barang = db_execute("SELECT b.*,u.nama as nama_pemilik,u.rating as rating_pemilik,u.id as pemilik_id FROM barang b JOIN users u ON b.id_pemilik=u.id WHERE b.id=?",(id,), fetchone=True)
    if not barang:
        flash('Barang tidak ditemukan.','error')
        return redirect(url_for('home'))
    reviews = db_execute("SELECT r.*,u.nama as nama_reviewer FROM review r JOIN users u ON r.id_reviewer=u.id JOIN transaksi t ON r.id_transaksi=t.id WHERE t.id_barang=? ORDER BY r.created_at DESC",(id,), fetchall=True)
    row = db_execute("SELECT COUNT(*) AS c FROM transaksi WHERE id_barang=? AND status NOT IN ('dibatalkan')",(id,), fetchone=True)
    total_disewa = row['c'] if row else 0
    db_execute("UPDATE barang SET total_disewa=? WHERE id=?",(total_disewa,id), commit=True)
    is_pemilik_sendiri = False
    if 'user_id' in session:
        is_pemilik_sendiri = (barang['id_pemilik'] == session['user_id']) or (session.get('role') == 'admin') or (session.get('tipe_akun') == 'pemilik')
    return render_template('detail_barang.html', barang=barang, reviews=reviews, total_disewa=total_disewa, notif_count=notif_count(), is_pemilik_sendiri=is_pemilik_sendiri)

@app.route('/booking/<int:id_barang>', methods=['GET','POST'])
def booking(id_barang):
    if 'user_id' not in session:
        return redirect(url_for('login', next=url_for('booking', id_barang=id_barang)))
    if cek_blokir(): return redirect(url_for('akun_diblokir'))
    if session.get('tipe_akun') in ['pemilik','admin'] or session.get('role') == 'admin':
        flash('Akun pemilik dan admin tidak dapat meminjam barang.','error')
        return redirect(url_for('detail_barang', id=id_barang))
    barang = db_execute("SELECT * FROM barang WHERE id=?",(id_barang,), fetchone=True)
    if not barang: return redirect(url_for('home'))
    if barang['id_pemilik'] == session['user_id']:
        flash('Kamu tidak bisa meminjam barang milikmu sendiri!','error')
        return redirect(url_for('detail_barang', id=id_barang))
    if request.method == 'POST':
        durasi = int(request.form['durasi'])
        metode_bayar = request.form['metode_pembayaran']
        biaya_sewa = barang['harga_sewa']
        subtotal = biaya_sewa * durasi
        biaya_admin = round(subtotal * 0.05)
        total_biaya = subtotal + biaya_admin
        db_execute("INSERT INTO transaksi (id_user,id_barang,tanggal_pinjam,durasi,metode_pengambilan,metode_pembayaran,biaya_sewa,total_biaya) VALUES (?,?,?,?,?,?,?,?)",
                   (session['user_id'],id_barang,request.form['tanggal_pinjam'],durasi,request.form['metode_pengambilan'],metode_bayar,biaya_sewa,total_biaya), commit=True)
        add_notif(barang['id_pemilik'],f"Ada permintaan booking untuk '{barang['nama_barang']}'!")
        flash('Permintaan peminjaman dikirim! Menunggu persetujuan pemilik.','success')
        return redirect(url_for('riwayat'))
    return render_template('booking.html', barang=barang, notif_count=notif_count())

@app.route('/riwayat')
def riwayat():
    if 'user_id' not in session: return redirect(url_for('login'))
    if cek_blokir(): return redirect(url_for('akun_diblokir'))
    if session.get('role') == 'admin': return redirect(url_for('admin_dashboard'))
    if session.get('tipe_akun') == 'pemilik':
        return render_template('riwayat_pemilik_info.html', notif_count=notif_count())
    transaksi = db_execute("""
        SELECT t.*,b.nama_barang,b.foto,b.harga_sewa,u.nama as nama_pemilik
        FROM transaksi t JOIN barang b ON t.id_barang=b.id JOIN users u ON b.id_pemilik=u.id
        WHERE t.id_user=? ORDER BY t.created_at DESC
    """,(session['user_id'],), fetchall=True)
    reviews_done = [r['id_transaksi'] for r in db_execute(
        "SELECT id_transaksi FROM review WHERE id_reviewer=?",(session['user_id'],), fetchall=True)]
    laporan_done = [l['id_transaksi'] for l in db_execute(
        "SELECT id_transaksi FROM laporan WHERE id_pelapor=? AND tipe_pelapor='peminjam'",(session['user_id'],), fetchall=True)]
    # Ambil denda aktif untuk peminjam ini
    denda_aktif = db_execute("""
        SELECT l.*,b.nama_barang
        FROM laporan l JOIN transaksi t ON l.id_transaksi=t.id
        JOIN barang b ON t.id_barang=b.id
        WHERE t.id_user=? AND l.tipe_pelapor='pemilik' AND l.status='selesai'
        AND l.total_tagihan IS NOT NULL AND l.total_tagihan > 0
        ORDER BY l.created_at DESC
    """,(session['user_id'],), fetchall=True)
    return render_template('riwayat.html', transaksi=transaksi,
                           reviews_done=reviews_done, laporan_done=laporan_done,
                           denda_aktif=denda_aktif, notif_count=notif_count())

@app.route('/pembayaran/<int:id_transaksi>', methods=['GET','POST'])
def pembayaran(id_transaksi):
    if 'user_id' not in session: return redirect(url_for('login'))
    if cek_blokir(): return redirect(url_for('akun_diblokir'))
    transaksi = db_execute("SELECT t.*,b.nama_barang,b.foto FROM transaksi t JOIN barang b ON t.id_barang=b.id WHERE t.id=? AND t.id_user=?",(id_transaksi,session['user_id']), fetchone=True)
    if not transaksi: return redirect(url_for('riwayat'))
    if request.method == 'POST':
        metode = transaksi['metode_pembayaran']
        if metode and metode.lower() in ['cash','cod','tunai']:
            db_execute("UPDATE transaksi SET status='sedang_dipinjam' WHERE id=?",(id_transaksi,), commit=True)
            add_notif(transaksi['id_user'],f"Pembayaran '{transaksi['nama_barang']}' (Cash) dikonfirmasi. Selamat meminjam!")
            flash('Pembayaran cash dikonfirmasi! Peminjaman aktif.','success')
        else:
            bukti = save_foto(request.files.get('bukti_pembayaran'), f'bukti_{id_transaksi}')
            db_execute("UPDATE transaksi SET status='menunggu_verifikasi',bukti_pembayaran=? WHERE id=?",(bukti,id_transaksi), commit=True)
            flash('Bukti pembayaran dikirim! Menunggu verifikasi admin.','success')
        return redirect(url_for('riwayat'))
    return render_template('pembayaran.html', transaksi=transaksi, notif_count=notif_count())

@app.route('/ajukan_pengembalian/<int:id_transaksi>', methods=['POST'])
def ajukan_pengembalian(id_transaksi):
    if 'user_id' not in session: return redirect(url_for('login'))
    t = db_execute("SELECT t.*,b.nama_barang,b.id_pemilik FROM transaksi t JOIN barang b ON t.id_barang=b.id WHERE t.id=? AND t.id_user=?",(id_transaksi,session['user_id']), fetchone=True)
    if not t or t['status'] != 'sedang_dipinjam':
        flash('Tidak bisa mengajukan pengembalian.','error')
        return redirect(url_for('riwayat'))
    db_execute("UPDATE transaksi SET status='menunggu_pengembalian' WHERE id=?",(id_transaksi,), commit=True)
    add_notif(t['id_pemilik'],f"Peminjam mengajukan pengembalian untuk '{t['nama_barang']}'. Silakan konfirmasi.")
    flash('Pengembalian diajukan! Menunggu konfirmasi pemilik.','success')
    return redirect(url_for('riwayat'))

@app.route('/checkin_barang/<int:id_transaksi>', methods=['GET','POST'])
def checkin_barang(id_transaksi):
    if 'user_id' not in session: return redirect(url_for('login'))
    if cek_blokir(): return redirect(url_for('akun_diblokir'))
    t = db_execute("""
        SELECT t.*,b.nama_barang,b.foto_serah,b.foto as foto_barang
        FROM transaksi t JOIN barang b ON t.id_barang=b.id
        WHERE t.id=? AND t.id_user=?
    """, (id_transaksi, session['user_id']), fetchone=True)
    if not t or t['status'] != 'sedang_dipinjam':
        flash('Tidak bisa melakukan checkin.', 'error')
        return redirect(url_for('riwayat'))
    if t['checkin_status']:
        flash('Kamu sudah melakukan verifikasi penerimaan untuk transaksi ini.', 'error')
        return redirect(url_for('riwayat'))
    if request.method == 'POST':
        status = request.form.get('checkin_status')
        catatan = request.form.get('checkin_catatan', '')
        foto = save_foto(request.files.get('foto_checkin'), f'checkin_{id_transaksi}')
        if not foto:
            flash('Foto wajib diupload sebagai bukti penerimaan.', 'error')
            return render_template('checkin_barang.html', transaksi=t, notif_count=notif_count())
        db_execute("""
            UPDATE transaksi SET foto_checkin=?, checkin_status=?, checkin_catatan=?, checkin_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (foto, status, catatan, id_transaksi), commit=True)
        pemilik = db_execute("SELECT id_pemilik FROM barang WHERE id=?", (t['id_barang'],), fetchone=True)
        if status == 'sesuai':
            add_notif(pemilik['id_pemilik'], f"Peminjam mengkonfirmasi barang '{t['nama_barang']}' diterima dalam kondisi sesuai.")
            flash('Konfirmasi penerimaan berhasil! Selamat menikmati.', 'success')
        else:
            add_notif(pemilik['id_pemilik'], f"⚠️ Peminjam melaporkan kondisi '{t['nama_barang']}' tidak sesuai saat diterima!")
            flash('Laporan ketidaksesuaian terkirim ke pemilik.', 'warning')
        return redirect(url_for('riwayat'))
    return render_template('checkin_barang.html', transaksi=t, notif_count=notif_count())

@app.route('/laporan/<int:id_transaksi>', methods=['GET','POST'])
def laporan(id_transaksi):
    if 'user_id' not in session: return redirect(url_for('login'))
    transaksi = db_execute("SELECT t.*,b.nama_barang,b.id_pemilik FROM transaksi t JOIN barang b ON t.id_barang=b.id WHERE t.id=?",(id_transaksi,), fetchone=True)
    if not transaksi: return redirect(url_for('riwayat'))
    if request.method == 'POST':
        jenis,deskripsi = request.form['jenis_masalah'],request.form['deskripsi']
        foto = save_foto(request.files.get('foto_bukti'), f'laporan_{id_transaksi}')
        db_execute("INSERT INTO laporan (id_pelapor,id_transaksi,jenis_masalah,deskripsi,foto_bukti,tipe_pelapor) VALUES (?,?,?,?,?,'peminjam')",(session['user_id'],id_transaksi,jenis,deskripsi,foto), commit=True)
        add_notif(transaksi['id_pemilik'],f"Ada laporan masalah dari peminjam untuk '{transaksi['nama_barang']}'.")
        flash('Laporan dikirim ke pemilik!','success')
        return redirect(url_for('riwayat'))
    return render_template('laporan.html', transaksi=transaksi, notif_count=notif_count())

@app.route('/review/<int:id_transaksi>', methods=['GET','POST'])
def review(id_transaksi):
    if 'user_id' not in session: return redirect(url_for('login'))
    transaksi = db_execute("SELECT t.*,b.nama_barang,b.id as id_barang FROM transaksi t JOIN barang b ON t.id_barang=b.id WHERE t.id=? AND t.id_user=?",(id_transaksi,session['user_id']), fetchone=True)
    if not transaksi: return redirect(url_for('riwayat'))
    if request.method == 'POST':
        rating,komentar = int(request.form['rating']),request.form['komentar']
        db_execute("INSERT INTO review (id_transaksi,id_reviewer,rating,komentar) VALUES (?,?,?,?)",(id_transaksi,session['user_id'],rating,komentar), commit=True)
        avg_row = db_execute("SELECT AVG(r.rating) AS c FROM review r JOIN transaksi t ON r.id_transaksi=t.id WHERE t.id_barang=?",(transaksi['id_barang'],), fetchone=True)
        avg = avg_row['c'] if avg_row else 0
        db_execute("UPDATE barang SET rating=? WHERE id=?",(avg,transaksi['id_barang']), commit=True)
        flash('Review berhasil dikirim!','success')
        return redirect(url_for('riwayat'))
    return render_template('review.html', transaksi=transaksi, notif_count=notif_count())

@app.route('/profil')
def profil():
    if 'user_id' not in session: return redirect(url_for('login'))
    if cek_blokir(): return redirect(url_for('akun_diblokir'))
    user = db_execute("SELECT * FROM users WHERE id=?",(session['user_id'],), fetchone=True)
    notifikasi = db_execute("SELECT * FROM notifikasi WHERE id_user=? ORDER BY created_at DESC LIMIT 10",(session['user_id'],), fetchall=True)
    db_execute("UPDATE notifikasi SET dibaca=1 WHERE id_user=?",(session['user_id'],), commit=True)
    rating_peminjam = db_execute("SELECT AVG(rating) as avg_r, COUNT(*) as cnt FROM review_peminjam WHERE id_peminjam=?",(session['user_id'],), fetchone=True)
    ulasan_peminjam = db_execute("""
        SELECT rp.*,u.nama as nama_pemilik,b.nama_barang FROM review_peminjam rp
        JOIN users u ON rp.id_pemilik=u.id JOIN transaksi t ON rp.id_transaksi=t.id
        JOIN barang b ON t.id_barang=b.id WHERE rp.id_peminjam=? ORDER BY rp.created_at DESC LIMIT 5
    """,(session['user_id'],), fetchall=True)
    return render_template('profil.html', user=user, notif_count=0, notifikasi=notifikasi,
                           rating_peminjam=rating_peminjam, ulasan_peminjam=ulasan_peminjam)

@app.route('/edit_profil', methods=['GET','POST'])
def edit_profil():
    if 'user_id' not in session: return redirect(url_for('login'))
    if cek_blokir(): return redirect(url_for('akun_diblokir'))
    user = db_execute("SELECT * FROM users WHERE id=?",(session['user_id'],), fetchone=True)
    if request.method == 'POST':
        nama,no_hp,alamat = request.form['nama'],request.form['no_hp'],request.form['alamat']
        db_execute("UPDATE users SET nama=?,no_hp=?,alamat=? WHERE id=?",(nama,no_hp,alamat,session['user_id']), commit=True)
        session['nama'] = nama
        flash('Profil berhasil diperbarui!','success')
        return redirect(url_for('profil'))
    return render_template('edit_profil.html', user=user, notif_count=notif_count())

@app.route('/pengaturan', methods=['GET','POST'])
def pengaturan():
    if 'user_id' not in session: return redirect(url_for('login'))
    if cek_blokir(): return redirect(url_for('akun_diblokir'))
    if request.method == 'POST':
        pw_lama,pw_baru,pw_konfirm = request.form['password_lama'],request.form['password_baru'],request.form['password_konfirm']
        user = db_execute("SELECT * FROM users WHERE id=?",(session['user_id'],), fetchone=True)
        if not check_password_hash(user['password'], pw_lama):
            flash('Password lama salah!','error')
        elif pw_baru != pw_konfirm:
            flash('Konfirmasi password tidak cocok!','error')
        elif len(pw_baru) < 6:
            flash('Password baru minimal 6 karakter!','error')
        else:
            db_execute("UPDATE users SET password=? WHERE id=?",(generate_password_hash(pw_baru),session['user_id']), commit=True)
            flash('Password berhasil diubah!','success')
    return render_template('pengaturan.html', notif_count=notif_count())

# ══════════════════════════════════════════════════
#  PEMILIK ROUTES
# ══════════════════════════════════════════════════

@app.route('/barang_saya')
def barang_saya():
    if 'user_id' not in session: return redirect(url_for('login'))
    if cek_blokir(): return redirect(url_for('akun_diblokir'))
    barang = db_execute("SELECT * FROM barang WHERE id_pemilik=? ORDER BY created_at DESC",(session['user_id'],), fetchall=True)
    bookings = db_execute("""
        SELECT t.*,b.nama_barang,u.nama as nama_peminjam,u.no_hp
        FROM transaksi t JOIN barang b ON t.id_barang=b.id JOIN users u ON t.id_user=u.id
        WHERE b.id_pemilik=? AND t.status='menunggu_persetujuan' ORDER BY t.created_at DESC
    """,(session['user_id'],), fetchall=True)
    aktif = db_execute("""
        SELECT t.*,b.nama_barang,u.nama as nama_peminjam,u.no_hp,u.id as id_peminjam
        FROM transaksi t JOIN barang b ON t.id_barang=b.id JOIN users u ON t.id_user=u.id
        WHERE b.id_pemilik=? AND t.status IN ('sedang_dipinjam','menunggu_pengembalian')
        ORDER BY t.created_at DESC
    """,(session['user_id'],), fetchall=True)
    selesai = db_execute("""
        SELECT t.*,b.nama_barang,u.nama as nama_peminjam
        FROM transaksi t JOIN barang b ON t.id_barang=b.id JOIN users u ON t.id_user=u.id
        WHERE b.id_pemilik=? AND t.status='selesai' ORDER BY t.created_at DESC LIMIT 20
    """,(session['user_id'],), fetchall=True)
    denda_done = [l['id_transaksi'] for l in db_execute(
        "SELECT id_transaksi FROM laporan WHERE id_pelapor=? AND tipe_pelapor='pemilik'",(session['user_id'],), fetchall=True)]
    laporan_masuk = db_execute("""
        SELECT l.*,u.nama as nama_peminjam,b.nama_barang
        FROM laporan l JOIN users u ON l.id_pelapor=u.id
        JOIN transaksi t ON l.id_transaksi=t.id JOIN barang b ON t.id_barang=b.id
        WHERE b.id_pemilik=? AND l.tipe_pelapor='peminjam' AND l.status='menunggu'
        ORDER BY l.created_at DESC
    """,(session['user_id'],), fetchall=True)
    reviews_peminjam_done = [r['id_transaksi'] for r in db_execute(
        "SELECT id_transaksi FROM review_peminjam WHERE id_pemilik=?",(session['user_id'],), fetchall=True)]
    # Laporan denda yang menunggu input nominal dari pemilik
    laporan_tunggu_nominal = db_execute("""
        SELECT l.id,l.id_transaksi,b.nama_barang,l.jenis_masalah,l.kategori_kerusakan
        FROM laporan l JOIN transaksi t ON l.id_transaksi=t.id
        JOIN barang b ON t.id_barang=b.id
        WHERE l.id_pelapor=? AND l.status='menunggu_harga'
        ORDER BY l.created_at DESC
    """,(session['user_id'],), fetchall=True)
    return render_template('barang_saya.html', barang=barang, bookings=bookings,
                           aktif=aktif, selesai=selesai, denda_done=denda_done,
                           laporan_masuk=laporan_masuk, reviews_peminjam_done=reviews_peminjam_done,
                           laporan_tunggu_nominal=laporan_tunggu_nominal,
                           notif_count=notif_count())

@app.route('/edit_barang/<int:id_barang>', methods=['GET','POST'])
def edit_barang(id_barang):
    if 'user_id' not in session: return redirect(url_for('login'))
    barang = db_execute("SELECT * FROM barang WHERE id=? AND id_pemilik=?",(id_barang,session['user_id']), fetchone=True)
    if not barang:
        flash('Barang tidak ditemukan atau bukan milikmu.','error')
        return redirect(url_for('barang_saya'))
    if request.method == 'POST':
        nama = request.form['nama_barang']
        kategori = request.form['kategori']
        harga = float(request.form['harga_sewa'])
        deskripsi = request.form['deskripsi']
        lokasi = request.form['lokasi']
        stok = int(request.form['stok'])
        foto = barang['foto']
        new_foto = save_foto(request.files.get('foto'), f'barang_{session["user_id"]}')
        if new_foto: foto = new_foto
        db_execute("UPDATE barang SET nama_barang=?,kategori=?,harga_sewa=?,deskripsi=?,lokasi=?,stok=?,foto=? WHERE id=?",
                   (nama,kategori,harga,deskripsi,lokasi,stok,foto,id_barang), commit=True)
        flash('Barang berhasil diperbarui!','success')
        return redirect(url_for('barang_saya'))
    return render_template('edit_barang.html', barang=barang, notif_count=notif_count())

def hitung_biaya_upload(harga_sewa):
    """Hitung biaya upload berdasarkan harga sewa per hari.
    Threshold berdasarkan harga sewa/hari bukan nilai barang."""
    if harga_sewa < 5000000: return 0
    elif harga_sewa < 10000000: return round(harga_sewa * 0.04)
    elif harga_sewa < 15000000: return round(harga_sewa * 0.06)
    elif harga_sewa < 20000000: return round(harga_sewa * 0.08)
    elif harga_sewa < 25000000: return round(harga_sewa * 0.10)
    else: return round(harga_sewa * 0.12)

@app.route('/upload_barang', methods=['GET','POST'])
def upload_barang():
    if 'user_id' not in session: return redirect(url_for('login'))
    if cek_blokir(): return redirect(url_for('akun_diblokir'))
    if request.method == 'POST':
        harga_sewa = float(request.form['harga_sewa'])
        biaya_upload = hitung_biaya_upload(harga_sewa)
        # Kalau ada biaya upload, redirect ke halaman konfirmasi pembayaran dulu
        if biaya_upload > 0:
            return render_template('upload_barang.html',
                notif_count=notif_count(),
                konfirmasi=True,
                form_data=request.form,
                biaya_upload=biaya_upload,
                harga_sewa=harga_sewa
            )
        # Kalau tidak ada biaya, langsung simpan
        foto = save_foto(request.files.get('foto'), f'barang_{session["user_id"]}')
        db_execute("INSERT INTO barang (nama_barang,kategori,harga_sewa,deskripsi,lokasi,stok,foto,id_pemilik) VALUES (?,?,?,?,?,?,?,?)",
                   (request.form['nama_barang'],request.form['kategori'],harga_sewa,request.form['deskripsi'],request.form['lokasi'],int(request.form['stok']),foto,session['user_id']), commit=True)
        flash('Barang berhasil diupload!','success')
        return redirect(url_for('barang_saya'))
    return render_template('upload_barang.html', notif_count=notif_count())

@app.route('/upload_barang/konfirmasi', methods=['POST'])
def upload_barang_konfirmasi():
    if 'user_id' not in session: return redirect(url_for('login'))
    harga_sewa = float(request.form['harga_sewa'])
    biaya_upload = hitung_biaya_upload(harga_sewa)
    foto = save_foto(request.files.get('foto'), f'barang_{session["user_id"]}')
    db_execute("INSERT INTO barang (nama_barang,kategori,harga_sewa,deskripsi,lokasi,stok,foto,id_pemilik) VALUES (?,?,?,?,?,?,?,?)",
               (request.form['nama_barang'],request.form['kategori'],harga_sewa,request.form['deskripsi'],request.form['lokasi'],int(request.form['stok']),foto,session['user_id']), commit=True)
    flash(f'Barang berhasil diupload! Biaya listing Rp {biaya_upload:,.0f} telah dicatat.','success')
    return redirect(url_for('barang_saya'))

@app.route('/setujui_booking/<int:id_transaksi>')
def setujui_booking(id_transaksi):
    if 'user_id' not in session: return redirect(url_for('login'))
    t = db_execute("SELECT t.*,b.nama_barang FROM transaksi t JOIN barang b ON t.id_barang=b.id WHERE t.id=?",(id_transaksi,), fetchone=True)
    db_execute("UPDATE transaksi SET status='menunggu_pembayaran' WHERE id=?",(id_transaksi,), commit=True)
    add_notif(t['id_user'],f"Booking '{t['nama_barang']}' disetujui! Silakan lakukan pembayaran.")
    flash('Booking disetujui!','success')
    return redirect(url_for('barang_saya'))

@app.route('/tolak_booking/<int:id_transaksi>')
def tolak_booking(id_transaksi):
    if 'user_id' not in session: return redirect(url_for('login'))
    t = db_execute("SELECT t.*,b.nama_barang FROM transaksi t JOIN barang b ON t.id_barang=b.id WHERE t.id=?",(id_transaksi,), fetchone=True)
    db_execute("UPDATE transaksi SET status='dibatalkan' WHERE id=?",(id_transaksi,), commit=True)
    add_notif(t['id_user'],f"Booking '{t['nama_barang']}' ditolak oleh pemilik.")
    flash('Booking ditolak.','success')
    return redirect(url_for('barang_saya'))

@app.route('/foto_serah/<int:id_transaksi>', methods=['GET','POST'])
def foto_serah(id_transaksi):
    if 'user_id' not in session: return redirect(url_for('login'))
    t = db_execute("SELECT t.*,b.nama_barang,b.id_pemilik FROM transaksi t JOIN barang b ON t.id_barang=b.id WHERE t.id=?",(id_transaksi,), fetchone=True)
    if not t or t['id_pemilik'] != session['user_id']: return redirect(url_for('barang_saya'))
    if request.method == 'POST':
        foto = save_foto(request.files.get('foto'), f'serah_{id_transaksi}')
        if foto:
            db_execute("UPDATE transaksi SET foto_serah=? WHERE id=?",(foto,id_transaksi), commit=True)
            add_notif(t['id_user'],f"Pemilik sudah memfoto kondisi '{t['nama_barang']}' sebelum diserahkan.")
            flash('Foto kondisi awal berhasil disimpan!','success')
        else:
            flash('Harap upload foto terlebih dahulu.','error')
        return redirect(url_for('barang_saya'))
    return render_template('foto_serah.html', transaksi=t, notif_count=notif_count())

@app.route('/konfirmasi_pengembalian/<int:id_transaksi>', methods=['GET','POST'])
def konfirmasi_pengembalian(id_transaksi):
    if 'user_id' not in session: return redirect(url_for('login'))
    t = db_execute("SELECT t.*,b.nama_barang,b.id_pemilik,b.id as id_barang FROM transaksi t JOIN barang b ON t.id_barang=b.id WHERE t.id=?",(id_transaksi,), fetchone=True)
    if not t or t['id_pemilik'] != session['user_id']: return redirect(url_for('barang_saya'))
    if request.method == 'POST':
        foto = save_foto(request.files.get('foto'), f'terima_{id_transaksi}')
        db_execute("UPDATE transaksi SET status='selesai', foto_terima=? WHERE id=?",(foto,id_transaksi), commit=True)
        db_execute("UPDATE users SET total_transaksi=total_transaksi+1 WHERE id=?",(t['id_user'],), commit=True)
        db_execute("UPDATE barang SET total_disewa=total_disewa+1 WHERE id=?",(t['id_barang'],), commit=True)
        add_notif(t['id_user'],f"Transaksi '{t['nama_barang']}' selesai! Mau beri ulasan barang ini?")
        add_notif(session['user_id'],f"Barang '{t['nama_barang']}' sudah kembali. Mau beri ulasan peminjam?")
        flash('Pengembalian dikonfirmasi. Transaksi selesai!','success')
        return redirect(url_for('barang_saya'))
    return render_template('konfirmasi_pengembalian.html', transaksi=t, notif_count=notif_count())

@app.route('/respon_laporan/<int:id_laporan>', methods=['POST'])
def respon_laporan(id_laporan):
    if 'user_id' not in session: return redirect(url_for('login'))
    l = db_execute("SELECT l.*,t.id_user,b.nama_barang FROM laporan l JOIN transaksi t ON l.id_transaksi=t.id JOIN barang b ON t.id_barang=b.id WHERE l.id=?",(id_laporan,), fetchone=True)
    respon = request.form['respon']
    db_execute("UPDATE laporan SET status='direspon', respon_pemilik=? WHERE id=?",(respon,id_laporan), commit=True)
    add_notif(l['id_user'],f"Pemilik merespon laporan '{l['nama_barang']}': {respon[:60]}...")
    flash('Respon berhasil dikirim ke peminjam!','success')
    return redirect(url_for('barang_saya'))

def hitung_fee_denda(nominal):
    tabel = [
        (100000, 0.04), (500000, 0.06), (1000000, 0.08),
        (2000000, 0.10), (3000000, 0.12), (5000000, 0.14),
        (7500000, 0.16), (10000000, 0.18), (12500000, 0.20),
        (15000000, 0.22), (17500000, 0.24), (20000000, 0.26),
    ]
    for batas, pct in tabel:
        if nominal < batas:
            return pct
    return 0.30

def kategori_dari_jenis(jenis):
    jenis = jenis.lower()
    if any(k in jenis for k in ['lecet','kotor','baret','kusut','noda']):
        return 'Ringan'
    elif any(k in jenis for k in ['rusak','pecah','bocor','patah','komponen','servis']):
        return 'Sedang'
    elif any(k in jenis for k in ['hilang','hancur','mati','fatal','tidak kembali','total']):
        return 'Berat'
    return 'Sedang'

@app.route('/denda/<int:id_transaksi>', methods=['GET','POST'])
def denda(id_transaksi):
    if 'user_id' not in session: return redirect(url_for('login'))
    transaksi = db_execute("""
        SELECT t.*,b.nama_barang,b.id_pemilik,u.nama as nama_peminjam
        FROM transaksi t JOIN barang b ON t.id_barang=b.id
        JOIN users u ON t.id_user=u.id
        WHERE t.id=?
    """, (id_transaksi,), fetchone=True)
    if not transaksi or transaksi['id_pemilik'] != session['user_id']:
        return redirect(url_for('barang_saya'))
    if transaksi['status'] != 'selesai':
        flash('Laporan denda hanya bisa diajukan setelah transaksi selesai.','error')
        return redirect(url_for('barang_saya'))
    if request.method == 'POST':
        jenis = request.form['jenis_masalah']
        deskripsi = request.form['deskripsi']
        nominal_pemilik = float(request.form.get('nominal_perbaikan', 0) or 0)
        foto = save_foto(request.files.get('foto_bukti'), f'denda_{id_transaksi}')
        kategori = kategori_dari_jenis(jenis)
        pct = hitung_fee_denda(nominal_pemilik)
        total_tagihan = round(nominal_pemilik * (1 + pct)) if nominal_pemilik > 0 else 0
        potongan = round(nominal_pemilik * pct) if nominal_pemilik > 0 else 0
        db_execute("""
            INSERT INTO laporan (id_pelapor,id_transaksi,jenis_masalah,deskripsi,
                foto_bukti,tipe_pelapor,kategori_kerusakan,nominal_pemilik,
                potongan_platform,total_tagihan,status_validasi)
            VALUES (?,?,?,?,?,'pemilik',?,?,?,?,'menunggu_validasi')
        """, (session['user_id'],id_transaksi,jenis,deskripsi,foto,
              kategori,nominal_pemilik,potongan,total_tagihan), commit=True)
        add_notif(transaksi['id_user'], f"Pemilik melaporkan kerusakan pada '{transaksi['nama_barang']}'. Menunggu validasi admin.")
        flash('Laporan denda dikirim! Menunggu validasi admin.','success')
        return redirect(url_for('barang_saya'))
    foto_serah = transaksi.get('foto_serah')
    foto_terima = transaksi.get('foto_terima')
    return render_template('denda.html', transaksi=transaksi,
                           foto_serah=foto_serah, foto_terima=foto_terima,
                           notif_count=notif_count())

@app.route('/input_nominal_denda/<int:id_laporan>', methods=['GET','POST'])
def input_nominal_denda(id_laporan):
    """Step 3: Pemilik input nominal biaya perbaikan setelah admin validasi"""
    if 'user_id' not in session: return redirect(url_for('login'))
    l = db_execute("""
        SELECT l.*,b.nama_barang,b.id_pemilik,t.id_user,t.foto_serah,t.foto_terima
        FROM laporan l JOIN transaksi t ON l.id_transaksi=t.id
        JOIN barang b ON t.id_barang=b.id WHERE l.id=?
    """, (id_laporan,), fetchone=True)
    if not l or l['id_pemilik'] != session['user_id']:
        return redirect(url_for('barang_saya'))
    if l['status'] != 'menunggu_harga':
        flash('Laporan ini tidak memerlukan input nominal.', 'error')
        return redirect(url_for('barang_saya'))
    if request.method == 'POST':
        nominal_pemilik = float(request.form['nominal_perbaikan'])
        pct = hitung_fee_denda(nominal_pemilik)
        potongan = round(nominal_pemilik * pct)
        total_tagihan = nominal_pemilik + potongan
        db_execute("""
            UPDATE laporan SET nominal_pemilik=?, potongan_platform=?,
            total_tagihan=?, status='menunggu_review' WHERE id=?
        """, (nominal_pemilik, potongan, total_tagihan, id_laporan), commit=True)
        add_notif(l['id_user'], f"Admin sedang mereview nominal denda untuk '{l['nama_barang']}'.")
        flash('Nominal dikirim! Menunggu review akhir admin.', 'success')
        return redirect(url_for('barang_saya'))
    return render_template('input_nominal_denda.html', laporan=l, notif_count=notif_count())

@app.route('/review_peminjam/<int:id_transaksi>', methods=['GET','POST'])
def review_peminjam(id_transaksi):
    if 'user_id' not in session: return redirect(url_for('login'))
    t = db_execute("""
        SELECT t.*,b.nama_barang,b.id_pemilik,u.nama as nama_peminjam
        FROM transaksi t JOIN barang b ON t.id_barang=b.id JOIN users u ON t.id_user=u.id
        WHERE t.id=?
    """,(id_transaksi,), fetchone=True)
    if not t or t['id_pemilik'] != session['user_id'] or t['status'] != 'selesai':
        return redirect(url_for('barang_saya'))
    sudah = db_execute("SELECT id FROM review_peminjam WHERE id_transaksi=? AND id_pemilik=?",(id_transaksi,session['user_id']), fetchone=True)
    if sudah:
        flash('Kamu sudah memberi ulasan untuk transaksi ini.','error')
        return redirect(url_for('barang_saya'))
    if request.method == 'POST':
        rating = int(request.form['rating'])
        komentar = request.form['komentar']
        db_execute("INSERT INTO review_peminjam (id_transaksi,id_pemilik,id_peminjam,rating,komentar) VALUES (?,?,?,?,?)",
                   (id_transaksi, session['user_id'], t['id_user'], rating, komentar), commit=True)
        avg_row = db_execute("SELECT AVG(rating) AS c FROM review_peminjam WHERE id_peminjam=?",(t['id_user'],), fetchone=True)
        avg = avg_row['c'] if avg_row else 0
        db_execute("UPDATE users SET rating=? WHERE id=?",(avg, t['id_user']), commit=True)
        add_notif(t['id_user'],f"Pemilik memberi ulasan untukmu sebagai peminjam '{t['nama_barang']}'.")
        flash('Ulasan peminjam berhasil dikirim!','success')
        return redirect(url_for('barang_saya'))
    return render_template('review_peminjam.html', transaksi=t, notif_count=notif_count())

# ══════════════════════════════════════════════════
#  ADMIN ROUTES
# ══════════════════════════════════════════════════

@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    if request.method == 'POST':
        email,password = request.form['email'],request.form['password']
        user = db_execute("SELECT * FROM users WHERE email=? AND role='admin'",(email,), fetchone=True)
        if user and check_password_hash(user['password'], password):
            session.update({'user_id':user['id'],'nama':user['nama'],'role':'admin','tipe_akun':'admin'})
            return redirect(url_for('admin_dashboard'))
        flash('Kredensial admin salah!','error')
    return render_template('admin/login.html')

@app.route('/admin')
def admin_dashboard():
    if session.get('role') != 'admin': return redirect(url_for('admin_login'))
    total_user = db_execute("SELECT COUNT(*) AS c FROM users WHERE role='user'", fetchone=True)['c']
    total_barang = db_execute("SELECT COUNT(*) AS c FROM barang", fetchone=True)["c"]
    total_transaksi = db_execute("SELECT COUNT(*) AS c FROM transaksi", fetchone=True)["c"]
    total_laporan = db_execute("SELECT COUNT(*) AS c FROM laporan WHERE status='menunggu'", fetchone=True)['c']
    total_banding = db_execute("SELECT COUNT(*) AS c FROM banding WHERE status='menunggu'", fetchone=True)['c']
    bayar_pending = db_execute("""
        SELECT t.*,b.nama_barang,u.nama as nama_user FROM transaksi t
        JOIN barang b ON t.id_barang=b.id JOIN users u ON t.id_user=u.id
        WHERE t.status='menunggu_verifikasi' ORDER BY t.created_at DESC
    """, fetchall=True)
    transaksi_recent = db_execute("""
        SELECT t.*,b.nama_barang,u.nama as nama_user FROM transaksi t
        JOIN barang b ON t.id_barang=b.id JOIN users u ON t.id_user=u.id
        ORDER BY t.created_at DESC LIMIT 8
    """, fetchall=True)
    return render_template('admin/dashboard.html', total_user=total_user, total_barang=total_barang,
                           total_transaksi=total_transaksi, total_laporan=total_laporan,
                           total_banding=total_banding, bayar_pending=bayar_pending,
                           transaksi_recent=transaksi_recent)

@app.route('/admin/profil', methods=['GET','POST'])
def admin_profil():
    if session.get('role') != 'admin': return redirect(url_for('admin_login'))
    user = db_execute("SELECT * FROM users WHERE id=?",(session['user_id'],), fetchone=True)
    if request.method == 'POST':
        aksi = request.form.get('aksi')
        if aksi == 'edit':
            db_execute("UPDATE users SET nama=? WHERE id=?",(request.form['nama'],session['user_id']), commit=True)
            session['nama'] = request.form['nama']
            flash('Profil diperbarui!','success')
        elif aksi == 'password':
            pw_lama,pw_baru = request.form['password_lama'],request.form['password_baru']
            if not check_password_hash(user['password'], pw_lama):
                flash('Password lama salah!','error')
            else:
                db_execute("UPDATE users SET password=? WHERE id=?",(generate_password_hash(pw_baru),session['user_id']), commit=True)
                flash('Password berhasil diubah!','success')
        return redirect(url_for('admin_profil'))
    return render_template('admin/profil.html', user=user)

@app.route('/admin/users')
def admin_users():
    if session.get('role') != 'admin': return redirect(url_for('admin_login'))
    users = db_execute("SELECT * FROM users WHERE role='user' ORDER BY created_at DESC", fetchall=True)
    ratings_raw = db_execute("SELECT id_peminjam, AVG(rating) as avg_r, COUNT(*) as cnt FROM review_peminjam GROUP BY id_peminjam", fetchall=True)
    ratings = {}
    for r in ratings_raw:
        ratings[r['id_peminjam']] = {'avg': round(r['avg_r'],1) if r['avg_r'] else 0, 'cnt': r['cnt']}
    return render_template('admin/users.html', users=users, ratings=ratings)

@app.route('/admin/blokir/<int:id>')
def admin_blokir(id):
    if session.get('role') != 'admin': return redirect(url_for('admin_login'))
    user = db_execute("SELECT status FROM users WHERE id=?",(id,), fetchone=True)
    new_status = 'diblokir' if user['status'] == 'aktif' else 'aktif'
    db_execute("UPDATE users SET status=? WHERE id=?",(new_status,id), commit=True)
    if new_status == 'aktif':
        db_execute("UPDATE banding SET status='diterima' WHERE id_user=? AND status='menunggu'",(id,), commit=True)
        add_notif(id,"Akun kamu telah diaktifkan kembali oleh admin.")
    return redirect(url_for('admin_users'))

@app.route('/admin/hapus_user/<int:id>')
def admin_hapus_user(id):
    if session.get('role') != 'admin': return redirect(url_for('admin_login'))
    db_execute("DELETE FROM users WHERE id=?",(id,), commit=True)
    return redirect(url_for('admin_users'))

@app.route('/admin/barang')
def admin_barang():
    if session.get('role') != 'admin': return redirect(url_for('admin_login'))
    barang = db_execute("SELECT b.*,u.nama as nama_pemilik FROM barang b JOIN users u ON b.id_pemilik=u.id ORDER BY b.created_at DESC", fetchall=True)
    return render_template('admin/barang.html', barang=barang)

@app.route('/admin/hapus_barang/<int:id>')
def admin_hapus_barang(id):
    if session.get('role') != 'admin': return redirect(url_for('admin_login'))
    db_execute("DELETE FROM barang WHERE id=?",(id,), commit=True)
    return redirect(url_for('admin_barang'))

@app.route('/admin/laporan')
def admin_laporan():
    if session.get('role') != 'admin': return redirect(url_for('admin_login'))
    laporan = db_execute("""
        SELECT l.*,u.nama as nama_pelapor,b.nama_barang,
               t.foto_serah,t.foto_terima
        FROM laporan l JOIN users u ON l.id_pelapor=u.id
        JOIN transaksi t ON l.id_transaksi=t.id
        JOIN barang b ON t.id_barang=b.id
        WHERE l.tipe_pelapor='pemilik'
        ORDER BY l.created_at DESC
    """, fetchall=True)
    return render_template('admin/laporan.html', laporan=laporan)

@app.route('/admin/validasi_laporan/<int:id>', methods=['POST'])
def admin_validasi_laporan(id):
    """Step 2: Admin validasi foto — setuju atau tolak"""
    if session.get('role') != 'admin': return redirect(url_for('admin_login'))
    aksi = request.form['aksi']
    alasan = request.form.get('alasan_tolak', '')
    l = db_execute("SELECT l.*,t.id_user,b.nama_barang,b.id_pemilik FROM laporan l JOIN transaksi t ON l.id_transaksi=t.id JOIN barang b ON t.id_barang=b.id WHERE l.id=?", (id,), fetchone=True)
    if aksi == 'setuju':
        db_execute("UPDATE laporan SET status_validasi='disetujui', status='menunggu_harga' WHERE id=?", (id,), commit=True)
        add_notif(l['id_pemilik'], f"✅ Laporan kerusakan '{l['nama_barang']}' divalidasi admin. Silakan masukkan nominal biaya perbaikan.")
        flash('Laporan disetujui. Pemilik akan diminta input nominal.', 'success')
    else:
        db_execute("UPDATE laporan SET status_validasi='ditolak', status='ditolak', alasan_tolak=? WHERE id=?", (alasan, id), commit=True)
        add_notif(l['id_pemilik'], f"❌ Laporan kerusakan '{l['nama_barang']}' ditolak admin. Alasan: {alasan}")
        add_notif(l['id_user'], f"Laporan kerusakan '{l['nama_barang']}' dari pemilik ditolak admin.")
        flash('Laporan ditolak.', 'success')
    return redirect(url_for('admin_laporan'))

@app.route('/admin/review_denda/<int:id>', methods=['POST'])
def admin_review_denda(id):
    """Step 5: Admin final review nominal dari pemilik"""
    if session.get('role') != 'admin': return redirect(url_for('admin_login'))
    aksi = request.form['aksi']
    alasan = request.form.get('alasan_tolak', '')
    l = db_execute("SELECT l.*,t.id_user,b.nama_barang,b.id_pemilik FROM laporan l JOIN transaksi t ON l.id_transaksi=t.id JOIN barang b ON t.id_barang=b.id WHERE l.id=?", (id,), fetchone=True)
    if aksi == 'setuju':
        db_execute("UPDATE laporan SET status='selesai', keputusan='Denda disetujui' WHERE id=?", (id,), commit=True)
        add_notif(l['id_user'], f"⚠️ Denda '{l['nama_barang']}' resmi diterbitkan. Total tagihan: Rp {l['total_tagihan']:,.0f}. Segera lunasi.")
        add_notif(l['id_pemilik'], f"✅ Nominal denda '{l['nama_barang']}' disetujui admin. Kamu akan menerima Rp {l['nominal_pemilik']:,.0f} setelah denda dibayar.")
        flash('Denda resmi diterbitkan!', 'success')
    else:
        db_execute("UPDATE laporan SET status='ditolak_nominal', keputusan='Nominal ditolak — terlalu tinggi', alasan_tolak=? WHERE id=?", (alasan, id), commit=True)
        add_notif(l['id_pemilik'], f"❌ Nominal denda '{l['nama_barang']}' ditolak admin. Alasan: {alasan}. Kamu bisa ajukan ulang dengan nominal yang wajar.")
        flash('Nominal ditolak. Pemilik bisa ajukan ulang.', 'success')
    return redirect(url_for('admin_laporan'))

@app.route('/admin/banding')
def admin_banding():
    if session.get('role') != 'admin': return redirect(url_for('admin_login'))
    banding = db_execute("SELECT b.*,u.nama,u.email,u.status as status_akun FROM banding b JOIN users u ON b.id_user=u.id ORDER BY b.created_at DESC", fetchall=True)
    return render_template('admin/banding.html', banding=banding)

@app.route('/admin/proses_banding/<int:id>/<aksi>')
def admin_proses_banding(id, aksi):
    if session.get('role') != 'admin': return redirect(url_for('admin_login'))
    b = db_execute("SELECT * FROM banding WHERE id=?",(id,), fetchone=True)
    if aksi == 'terima':
        db_execute("UPDATE users SET status='aktif' WHERE id=?",(b['id_user'],), commit=True)
        db_execute("UPDATE banding SET status='diterima' WHERE id=?",(id,), commit=True)
        add_notif(b['id_user'],"Banding kamu diterima! Akun sudah aktif kembali.")
    else:
        db_execute("UPDATE banding SET status='ditolak' WHERE id=?",(id,), commit=True)
        add_notif(b['id_user'],"Banding kamu ditolak oleh admin.")
    flash(f"Banding {'diterima' if aksi=='terima' else 'ditolak'}.","success")
    return redirect(url_for('admin_banding'))

@app.route('/admin/verifikasi_pembayaran/<int:id>')
def admin_verifikasi_pembayaran(id):
    if session.get('role') != 'admin': return redirect(url_for('admin_login'))
    t = db_execute("SELECT t.*,b.nama_barang FROM transaksi t JOIN barang b ON t.id_barang=b.id WHERE t.id=?",(id,), fetchone=True)
    db_execute("UPDATE transaksi SET status='sedang_dipinjam' WHERE id=?",(id,), commit=True)
    add_notif(t['id_user'],f"Pembayaran '{t['nama_barang']}' diverifikasi. Selamat meminjam!")
    flash('Pembayaran diverifikasi!','success')
    return redirect(url_for('admin_dashboard'))

# ══════════════════════════════════════════════════
#  CHAT ROUTES
# ══════════════════════════════════════════════════

@app.route('/chat')
def chat_list():
    if 'user_id' not in session: return redirect(url_for('login'))
    if session.get('role') == 'admin': return redirect(url_for('admin_dashboard'))
    if cek_blokir(): return redirect(url_for('akun_diblokir'))
    chats = db_execute("""
        SELECT
            CASE WHEN c.id_pengirim=? THEN c.id_penerima ELSE c.id_pengirim END as partner_id,
            u.nama as partner_nama,
            MAX(c.created_at) as last_time,
            c.pesan as last_pesan,
            SUM(CASE WHEN c.id_penerima=? AND c.dibaca=0 THEN 1 ELSE 0 END) as unread
        FROM chat c
        JOIN users u ON u.id = CASE WHEN c.id_pengirim=? THEN c.id_penerima ELSE c.id_pengirim END
        WHERE c.id_pengirim=? OR c.id_penerima=?
        GROUP BY partner_id, u.nama, c.pesan
        ORDER BY last_time DESC
    """,(session['user_id'],session['user_id'],session['user_id'],session['user_id'],session['user_id']), fetchall=True)
    return render_template('chat_list.html', chats=chats, notif_count=notif_count())

@app.route('/chat/<int:partner_id>', methods=['GET','POST'])
@app.route('/chat/<int:partner_id>/barang/<int:id_barang>', methods=['GET','POST'])
def chat_detail(partner_id, id_barang=None):
    if 'user_id' not in session: return redirect(url_for('login'))
    if session.get('role') == 'admin': return redirect(url_for('admin_dashboard'))
    if cek_blokir(): return redirect(url_for('akun_diblokir'))
    partner = db_execute("SELECT * FROM users WHERE id=?",(partner_id,), fetchone=True)
    if not partner: return redirect(url_for('chat_list'))
    barang = None
    if id_barang:
        barang = db_execute("SELECT * FROM barang WHERE id=?",(id_barang,), fetchone=True)
    if request.method == 'POST':
        pesan = request.form.get('pesan','').strip()
        if pesan:
            db_execute("INSERT INTO chat (id_pengirim,id_penerima,id_barang,pesan) VALUES (?,?,?,?)",
                       (session['user_id'],partner_id,id_barang,pesan), commit=True)
            add_notif(partner_id,f"Pesan baru dari {session['nama']}: {pesan[:50]}")
        return redirect(url_for('chat_detail', partner_id=partner_id))
    messages = db_execute("""
        SELECT c.*,u.nama as nama_pengirim,b.nama_barang as konteks_barang
        FROM chat c JOIN users u ON c.id_pengirim=u.id
        LEFT JOIN barang b ON c.id_barang=b.id
        WHERE (c.id_pengirim=? AND c.id_penerima=?) OR (c.id_pengirim=? AND c.id_penerima=?)
        ORDER BY c.created_at ASC
    """,(session['user_id'],partner_id,partner_id,session['user_id']), fetchall=True)
    db_execute("UPDATE chat SET dibaca=1 WHERE id_penerima=? AND id_pengirim=?",(session['user_id'],partner_id), commit=True)
    return render_template('chat_detail.html', partner=partner, messages=messages, barang=barang, id_barang=id_barang, notif_count=notif_count())

# ── HALAMAN STATIS ──
@app.route('/tentang')
def tentang():
    return render_template('tentang.html', notif_count=notif_count())

@app.route('/bantuan')
def bantuan():
    return render_template('bantuan.html', notif_count=notif_count())

if __name__ == '__main__':
    os.makedirs('static/images/uploads', exist_ok=True)
    with app.app_context():
        try:
            init_db()
        except Exception as e:
            print(f"[WARNING] init_db error: {e}")
    app.run(debug=True)
else:
    # Dijalankan oleh gunicorn (production)
    with app.app_context():
        try:
            init_db()
        except Exception as e:
            print(f"[WARNING] init_db error: {e}")
