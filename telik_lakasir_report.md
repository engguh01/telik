# Laporan Pengujian Skill `telik` — Studi Kasus Codebase Lakasir v1.1.11

**Tanggal pengujian:** 25 Juli 2026
**Skill diuji:** `telik` (scoper.py) — commit sesuai isi `telik-master.zip`
**Project uji:** `lakasir-1_1_11.zip` — Laravel 11 + Filament + Livewire, aplikasi POS (727 file setelah difilter `.gitignore` standar)

---

## 1. Ringkasan eksekutif

| Pertanyaan | Jawaban singkat |
|---|---|
| Apakah telik menghemat token dibanding tanpa telik? | **Ya, signifikan** — rata-rata **86%** lebih hemat token dibanding baseline grep-terarah, dan bisa tembus **~98%** kalau satu bug candidate-quality yang ditemukan di bawah diperbaiki. |
| Apakah telik langsung jalan mulus di project yang dikasih (zip mentah)? | **Tidak.** Di kondisi apa adanya (tanpa `git init`, tanpa `.gitignore` root), telik meng-index **24.733 file** (termasuk 24.041 file di `vendor/`), butuh **~87 detik** untuk build index pertama kali, dan pada prompt uji, **5 dari 5 kandidat yang dikembalikan adalah file dependency `vendor/fakerphp/faker`** — 0% relevan. |
| Ada bug candidate-quality yang ditemukan? | **Ya, 2 kelas bug konkret** (root cause + repro ada di §5), plus 1 keterbatasan struktural (import-graph nyaris tidak pernah aktif di codebase Laravel). |

Angka "40–95% hemat token" yang sudah ditulis ulang di README kamu **konsisten dengan hasil pengujian ini** — tapi laporan ini menunjukkan bagian bawah range itu (dan kadang di bawahnya) terjadi bukan karena "prompt kurang spesifik", melainkan karena bug spesifik yang bisa diperbaiki. Detail di §6.

---

## 2. Metodologi

### 2.1 Setup project

`lakasir-1_1_11.zip` yang di-upload **tidak berisi `.git/` maupun `.gitignore` di root** (hanya ada beberapa `.gitignore` di subfolder seperti `storage/`, `database/`, `bootstrap/cache/`). Karena telik punya dua jalur listing file yang perilakunya sangat berbeda tergantung ini, project diuji dalam **dua skenario**:

- **Skenario A — "apa adanya"**: zip di-extract langsung, tanpa `git init`, tanpa `.gitignore` root. ← *ini persis kondisi file yang dikirim.*
- **Skenario B — "project dev yang wajar"**: copy dari Skenario A, ditambah `git init` + `.gitignore` standar Laravel (exclude `/vendor`, `/node_modules`, `/storage/framework/*`, dst), lalu `git add && git commit`. Ini merepresentasikan kondisi project yang sudah dikelola dengan git — asumsi implisit yang dipakai contoh-contoh di README telik sendiri.

Skenario B dipakai sebagai baseline utama perbandingan token (karena itu kondisi "wajar"), Skenario A dilaporkan terpisah di §5.1 karena hasilnya jauh berbeda dan penting untuk didokumentasikan mengingat itu bentuk file yang literally dikasih ke saya.

### 2.2 Definisi baseline "tanpa telik"

Supaya perbandingan token apple-to-apple, baseline "tanpa telik" disimulasikan dengan cara yang sama persis dengan contoh di README kamu sendiri ("grep keyword → baca semua file yang match secara penuh"), dan pakai heuristik token yang **sama** dengan punya scoper.py sendiri (`ukuran_file_bytes / 4`):

1. Ambil kata benda paling dominan dari prompt (1–2 kata) — cara wajar seorang agent/dev yang tidak pakai scoping akan memilih kata kunci untuk grep, bukan grep semua kata dalam kalimat.
2. `grep -ril` kata itu ke seluruh file yang di-track git (727 file), exclude lockfile (`composer.lock`, `package-lock.json`) dan asset vendor terkompilasi karena itu jelas bukan target baca siapa pun.
3. Baca **seluruh isi** tiap file yang match (bukan sebagian) → jumlah token = total ukuran file / 4.

Sebagai pembanding tambahan (§6, kolom kedua), saya juga hitung baseline "grep semua kata non-stopword di prompt tanpa filter noise" — ini worst-case yang jauh lebih buruk, dilaporkan supaya kamu lihat rentang penuhnya, bukan cuma angka yang menguntungkan telik.

### 2.3 Daftar prompt uji

Lima prompt vibe-coding dipilih meniru **persis register bahasa** yang dipakai di contoh `SKILL.md` kamu sendiri ("ubah button di header", "samain style sama halaman login") — yaitu **code-switching** Indonesia-Inggris (istilah teknis tetap Inggris, kata kerja/penghubung Indonesia), bukan terjemahan penuh ke Bahasa Indonesia:

| # | Prompt | Fitur yang disasar |
|---|---|---|
| A | *"fix save button di halaman add product"* | Form tambah/edit produk |
| B | *"tambahin validasi stock pas checkout di cashier page"* | Validasi stok di kasir |
| C | *"samain style widget best selling product sama expired product"* | Dua widget dashboard |
| D | *"fix bug discount ga kehitung di cashier report"* | Perhitungan diskon di laporan kasir |
| E | *"samain card member style sama product resource"* | Konsistensi UI member vs produk |

Satu prompt tambahan ditulis **full Bahasa Indonesia** (*"benerin tombol simpan di halaman tambah produk"*) khusus untuk menguji batas kemampuan bahasa — hasilnya jadi temuan bug utama, lihat §5.2.

---

## 3. Alur kerja skill `telik` (as-built)

Diagram alurnya sudah ditampilkan di chat. Ringkasannya, per `SKILL.md`:

1. **Prompt masuk** — agent dilarang browsing manual (`ls -R`, `find`, `view` root) dulu.
2. **`scoper.py --scope "<prompt verbatim>"`** dijalankan. Index file di-build (gitignore-aware via `git ls-files`, atau fallback `os.walk` + parsing `.gitignore` kalau bukan repo git) atau dipakai ulang dari cache `.scoper_cache/tree_index.json` kalau masih fresh (fingerprint git HEAD+dirty count, atau mtime 5 menit untuk non-git).
3. **Scoring** tiap file dari 4 sinyal: filename/path match (tokenized, fuzzy), symbol match (regex per bahasa), git-hot boost (file yang baru diubah), session memory (prompt mirip sebelumnya). Threshold `min_score=0.45`.
4. **Output**: `candidates` (top match, default max 5) + `related_files` (1-hop import graph) + `token_estimate` per file + `warnings` (file/total besar).
5. **Agent membaca** hanya `candidates`, `related_files` cuma dibuka kalau benar-benar perlu. Kalau `candidates` kosong/ambigu, agent disuruh **tanya user**, bukan fallback scan manual.

Desainnya sendiri sudah bagus dan disiplin — masalah yang saya temukan semua ada di *implementasi scoring/listing*, bukan di alur/desain skill-nya.

---

## 4. Hasil — Skenario B (project dengan git + `.gitignore`)

`total_files_indexed = 727` untuk semua prompt di bawah. Baseline = grep 1–2 kata kunci dominan, baca penuh semua file yang match (lihat §2.2).

| # | Prompt | Kandidat telik (file) | Token telik | File baseline | Token baseline | Hemat token |
|---|---|---|---|---|---|---|
| A | fix save button di halaman add product | 5 | 6.121 | 180 | 588.059 | **98,96%** |
| B | tambahin validasi stock pas checkout di cashier page | 5 | 4.395 | 112 | 119.354 | **96,32%** |
| C | samain style widget best selling product sama expired product | 5 | 103.449 ⚠️ | 170 | 443.610 | 76,68% *(rusak — lihat §5.3)* |
| D | fix bug discount ga kehitung di cashier report | 5 | 4.594 | 52 | 85.541 | **94,63%** |
| E | samain card member style sama product resource | 5 | 114.233 ⚠️ | 207 | 457.599 | 75,03% *(rusak — lihat §5.3)* |
| **Total** | | **25** | **232.792** | **721** | **1.694.163** | **86,26%** |

Kalau bug di §5.3 (vendor bundle ketarik jadi kandidat) diperbaiki — dihitung dengan membuang 1 file penyebab di prompt C dan E — total token telik turun ke **26.090**, dan hemat token naik ke **98,46%**. Jadi ada gap ~12 poin persentase yang murni disebabkan satu bug yang bisa diperbaiki, bukan keterbatasan pendekatan scoping itu sendiri.

Reduksi jumlah file yang perlu dibaca konsisten tinggi di semua prompt: rata-rata **95,6% lebih sedikit file dibuka** (5 file vs rata-rata ~114 file/prompt).

---

## 5. Temuan & bug (root cause + repro)

### 5.1 KRITIS — Tanpa git/`.gitignore`, telik meng-index seluruh `vendor/` dan hasilnya 100% sampah

**Yang terjadi:** dijalankan persis ke zip yang dikirim (tanpa `git init`, tanpa `.gitignore` root):

```
total_files_indexed: 24.733   (vs 727 di Skenario B — 34x lebih banyak)
waktu build index (cold):  86,7 detik  (vs 1,9 detik — 45x lebih lambat)
```

Prompt yang sama (*"fix save button di halaman add product"*) mengembalikan:

```json
"candidates": [
  "vendor/fakerphp/faker/src/Faker/Provider/HtmlLorem.php",
  "vendor/fakerphp/faker/src/Faker/Generator.php",
  "vendor/fakerphp/faker/src/Faker/Provider/fa_IR/Address.php",
  "vendor/fakerphp/faker/src/Faker/Provider/uk_UA/Address.php",
  "vendor/fakerphp/faker/src/Faker/ORM/CakePHP/Populator.php"
]
```

**5 dari 5 kandidat adalah dependency pihak ketiga (FakerPHP), 0% relevan** dengan kode aplikasi lakasir sendiri.

**Root cause:** `scripts/scoper.py` baris 78–81:

```python
FALLBACK_IGNORE_DIRS: Set[str] = {
    ".git", "node_modules", "dist", "build", ".next", ".cache",
    "venv", ".venv", "__pycache__", ".scoper_cache",
}
```

Daftar ini murni ekosistem JS/Python. **`vendor/`** (konvensi standar Composer/PHP, juga dipakai Ruby Bundler dan Go) tidak ada di daftar. Kalau project bukan repo git (jadi lewat `list_files_fallback`, baris 262) dan tidak ada `.gitignore` di root, seluruh isi `vendor/` (24.041 file di kasus ini) ikut ter-index dan jadi kandidat sah menurut scorer.

**Repro:**
```bash
cd lakasir-1_1_11   # zip di-extract apa adanya, jangan git init dulu
python3 <telik>/scripts/scoper.py --root . --scope "fix save button di halaman add product"
```

**Dampak:** ini bukan cuma soal kualitas — project PHP/Composer, Ruby/Bundler, atau Go modules manapun yang belum di-git-init atau belum punya `.gitignore` root akan mengalami ini. Dan situasi ini **bukan skenario langka**: persis kondisi file zip yang saya terima untuk pengujian ini.

---

### 5.2 KRITIS — Matching berbasis containment string pendek gampang salah tangkap kalau prompt full Bahasa Indonesia

**Yang terjadi:** prompt *"benerin tombol simpan di halaman tambah produk"* (murni kosakata Indonesia, tanpa code-switch) menghasilkan:

```json
"candidates": [
  "public/images/icons/splash_screens/11__iPad_Pro_M4_landscape.png",
  "public/images/icons/splash_screens/11__iPad_Pro_M4_portrait.png",
  "public/images/icons/splash_screens/11__iPad_Pro__10.5__iPad_Pro_landscape.png",
  "public/images/icons/splash_screens/11__iPad_Pro__10.5__iPad_Pro_portrait.png",
  "public/images/icons/splash_screens/12.9__iPad_Pro_landscape.png"
],
"warnings": ["Total estimated context across candidates is ~130117 tokens..."]
```

Lima kandidat, semuanya gambar splash-screen PWA untuk iPad — **tidak ada satupun kode aplikasi**, dan total estimasi tokennya (130K) lebih besar dari baseline grep-fokus manapun di laporan ini. Kalau agent percaya begitu saja pada `candidates`, ini lebih buruk daripada tidak pakai telik sama sekali.

**Root cause:** `text_match_score()` (baris 909–927) punya tingkatan fallback: exact token match (0.95) → *containment* — `kw in tok or tok in kw` (0.8) → substring di full path (0.7) → fuzzy ratio (≥0.75). Kata kunci `"produk"` tidak match apapun secara semantik, tapi lolos di tingkat *containment*: token `"pro"` (dari `iPad_Pro`, dipisah underscore di nama file splash-screen ala Apple) adalah substring literal dari `"produk"` (`pro` + `duk`). Karena tidak ada kata kunci lain di prompt yang match apapun di seluruh 727 file (kosakata Indonesia penuh — *benerin, tombol, simpan, halaman, tambah* — tidak match kode yang penamaannya Inggris), file-file splash-screen ini "menang" begitu saja karena tidak ada saingan skor lain.

**Repro:**
```bash
python3 <telik>/scripts/scoper.py --root lakasir_git --scope "benerin tombol simpan di halaman tambah produk"
```

**Pola yang sama juga muncul di prompt B** (versi code-switch): kata `"pas"` (Indonesia, artinya "saat/tepat") ⊂ `"password"`, sehingga `ResetPassword.php`, `PasswordResetLinkController.php`, `lang/en/passwords.php` ikut nyangkut sebagai kandidat — padahal prompt sama sekali tidak menyinggung password.

**Kenapa ini penting khusus buat telik:** SKILL.md kamu sendiri kasih contoh trigger dalam Bahasa Indonesia ("ubah button di header", "samain style..."). Artinya target pengguna skill ini memang akan menulis prompt campur/​​full Indonesia. Tidak ada layer sinonim Indonesia↔Inggris sama sekali di scorer — satu-satunya jalan sebuah kata Indonesia bisa match kode berbahasa Inggris adalah kebetulan string, dan kebetulan string kadang salah (`produk`→`pro`, `pas`→`password`) dan kadang random-benar (`stok`→lolos lewat kata `checkout` yang match `Check...Stock`, bukan lewat `stok` itu sendiri).

---

### 5.3 TINGGI — File vendor/bundle JS besar bisa ketarik jadi kandidat lewat 1 keyword generik, dan biayanya sangat mahal

**Yang terjadi:** prompt C dan E, yang sebenarnya menghasilkan kandidat bagus di 4 dari 5 slot, masing-masing kebobolan **1 file**:

- Prompt C → `public/vendor/livewire/livewire.js` (**96.813 token** — untuk 1 file)
- Prompt E → `public/vendor/livewire/livewire.esm.js` (**109.889 token** — untuk 1 file)

Ini adalah file JS pihak ketiga (bundle Livewire, sudah ada di repo karena Laravel package publish asset-nya ke `public/vendor/`), bukan kode yang pernah dimaksud user untuk diedit.

**Root cause (dikonfirmasi via debug langsung ke index):** untuk prompt E, symbol yang diekstrak dari `livewire.esm.js` termasuk `normalizeStyle`, `stringifyStyle`, `parseStringStyle` (utility yang ikut ter-bundle dari reactivity library di dalamnya). Kata kunci prompt `"style"` **match persis (skor 0.95)** ke token `"style"` di dalam nama symbol `normalizeStyle`. Ini bukan bug fuzzy-matching seperti §5.2 — ini match yang sah secara literal, tapi jadi masalah karena:

1. File bundle/minified punya puluhan-ratusan symbol generik → peluang satu di antaranya kebetulan match kata umum ("style", "card", "product", dst.) jauh lebih tinggi dibanding file kode aplikasi biasa.
2. Begitu satu symbol match, **skor tidak dibobot ulang berdasar ukuran file** — file 439KB (109K token) dan file 50-baris (200 token) diperlakukan setara di tahap scoring, padahal `token_estimate`/`warnings` yang menjelaskan konsekuensinya baru dihitung *setelah* file itu sudah lolos jadi candidate.

**Repro:**
```bash
python3 <telik>/scripts/scoper.py --root lakasir_git --scope "samain card member style sama product resource"
# lihat public/vendor/livewire/livewire.esm.js di candidates, token_estimate ~109889
```

**Dampak:** karena `public/vendor/` tetap ter-*track* git (memang sengaja di-commit oleh package Laravel), `.gitignore` saja tidak cukup untuk mencegah ini — perlu penanganan eksplisit.

---

### 5.4 SEDANG — `related_files` (import graph) nyaris tidak pernah aktif untuk project Laravel/PHP modern

**Yang terjadi:** di seluruh 5 prompt uji Skenario B, `related_files` **selalu kosong** — padahal fitur ini didesain untuk menangkap file yang secara struktural terhubung (mis. file yang di-import candidate).

**Root cause:** `resolve_php_import()` (baris 678–697) untuk import gaya namespace (`use App\Models\Tenants\Product;` — cara standar Laravel/PSR-4, dipakai di hampir semua file PHP modern) mencoba resolve dengan:

```python
PHP_SOURCE_DIRS = ["", "src/", "lib/", "app/"]
...
path = raw_import.replace("\\", "/") + ".php"   # -> "App/Models/Tenants/Product.php"
for source_dir in PHP_SOURCE_DIRS:
    c = source_dir + path                        # "" + "App/Models/..." (huruf App besar, case-sensitive, tidak match "app/Models/...")
                                                   # "app/" + "App/Models/..." (nested salah, jadi "app/App/Models/...")
```

Composer PSR-4 memetakan namespace root `App\` (huruf besar) ke folder `app/` (huruf kecil) lewat `composer.json`. Resolver ini tidak baca `composer.json` sama sekali dan tidak menormalkan case, jadi **hampir semua `use` statement di codebase Laravel gagal di-resolve** — bukan cuma di lakasir, ini generik untuk hampir semua project Laravel/Symfony modern yang pakai PSR-4 autoload (mayoritas ekosistem PHP saat ini).

**Dampak:** fitur "related_files via import graph" yang diklaim di README (poin 5 dari 5 sinyal) secara praktis **tidak aktif** untuk keluarga bahasa PHP dengan autoloading namespace — hanya berfungsi untuk import relatif gaya lama (`require_once './foo.php'`).

---

## 6. Rentang hemat token — versi yang bisa dipertanggungjawabkan

| Skenario | Hemat token (rata-rata 5 prompt) |
|---|---|
| Baseline grep-semua-kata (worst-case naive, tanpa filter noise) | **98,13%** |
| Baseline grep-fokus 1–2 kata dominan (baseline "wajar" — dipakai di §4) | **86,26%** |
| Baseline grep-fokus, **setelah bug §5.3 diperbaiki** | **98,46%** |
| Skenario A — zip mentah tanpa git/.gitignore | **negatif secara efektif** (5/5 kandidat 0% relevan → biaya round-trip tambahan untuk cari ulang, terlepas dari angka token mentahnya kecil) |

Rentang **40–95%** yang sekarang ada di README **tidak salah**, tapi laporan ini kasih tahu kenapa angka di bagian bawah range itu muncul: bukan melulu "prompt kurang spesifik" seperti tertulis sekarang, tapi juga karena bug konkret di §5.2/§5.3. Saran redaksi README: sebutkan eksplisit bahwa hasil bergantung pada (a) project sudah punya git+`.gitignore`, dan (b) prompt tidak murni Bahasa Indonesia tanpa istilah teknis Inggris.

---

## 7. Rekomendasi perbaikan (urut prioritas)

1. **Tambahkan `vendor` ke default-exclude fallback**, dan idealnya deteksi konvensi umum lain (`Pods`, `target`, `.bundle`) — supaya Skenario A tidak terjadi lagi. Ini perbaikan 1 baris (`FALLBACK_IGNORE_DIRS`) dengan dampak besar. (§5.1)
2. **Turunkan bobot atau exclude file besar/vendor dari kandidat**, terlepas dari match: kalau path cocok pola `public/vendor/`, `*.min.js`, atau file akan kena `TOKEN_WARN_FILE_THRESHOLD`, jangan biarkan 1 keyword generik meloloskannya sebagai candidate — turunkan skornya alih-alih dianggap sama dengan file kode biasa. (§5.3)
3. **Perketat aturan containment matching**: syaratkan panjang minimum (mis. `len(tok) >= 4` dan `len(kw) >= 4`) sebelum containment 2 arah (`kw in tok or tok in kw`) dipakai, supaya kata pendek kebetulan (`pro`, `pas`, `out`) berhenti nge-trigger match ke kata yang tidak nyambung secara semantik. (§5.2)
4. **Baca `composer.json` (`autoload.psr-4`)** untuk resolve `use Namespace\Class;` PHP ke path file sebenarnya, supaya `related_files` benar-benar berfungsi di project Laravel/Symfony. (§5.4)
5. **Tambahkan lapisan sinonim Indonesia↔Inggris** untuk kosakata vibe-coding yang paling umum (`produk↔product`, `kasir↔cashier`, `tombol↔button`, `halaman↔page`, `simpan↔save`, `tambah↔add`, `hapus↔delete`, `ubah↔edit/update`) — karena SKILL.md sendiri menyasar prompt Indonesia, tapi scorer saat ini nol pemahaman bahasa di luar kebetulan string. (§5.2)
6. **Beri sinyal confidence ke agent** saat SEMUA kandidat cuma lolos lewat tingkat fallback containment/fuzzy (bukan exact token/symbol match) — supaya instruksi "Step 2: judge candidates" di SKILL.md punya sinyal konkret untuk dipakai, bukan cuma "kalau kelihatan aneh".

---

## 8. Lampiran — data mentah

Semua angka di laporan ini reproducible. Ringkasan kandidat + token per prompt (Skenario B):

```
A) fix save button di halaman add product
   candidates: GeneralSetting.php(2829) EditProduct.php(938) Barcode.php(746)
               CartInteraction.php(1073) AssignProduct.php(535)  -> total 6.121 token

B) tambahin validasi stock pas checkout di cashier page
   candidates: Product.php(2302) StockService.php(653) CheckProductStock.php(392)
               Stock.php(903) StockOpname.php(145)  -> total 4.395 token

C) samain style widget best selling product sama expired product
   candidates: Product.php(2302) SellingOverview.php(1365) ProductResource.php(2499)
               livewire.js(96813) ViewSelling.php(470)  -> total 103.449 token

D) fix bug discount ga kehitung di cashier report
   candidates: CartItem.php(593) User.php(682) SellingOverview.php(1365)
               CashierReportController.php(269) CashierReportService.php(1685)  -> total 4.594 token

E) samain card member style sama product resource
   candidates: Barcode.php(746) livewire.esm.js(109889) AssignProduct.php(535)
               HasTranslatableResource.php(761) Product.php(2302)  -> total 114.233 token
```

`related_files` kosong di kelima prompt (lihat §5.4).

**Perintah reproduksi (dari root `telik-master/`):**
```bash
# Skenario B (git init + .gitignore Laravel standar dulu di project target)
python3 scripts/scoper.py --root <lakasir_git> --build-index
python3 scripts/scoper.py --root <lakasir_git> --scope "fix save button di halaman add product"

# Skenario A (persis zip apa adanya, tanpa git/.gitignore)
python3 scripts/scoper.py --root <lakasir_raw> --build-index
```
