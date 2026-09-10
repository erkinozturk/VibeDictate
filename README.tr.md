# vibedictate

**English:** [README.md](README.md)

Basılı-tut-konuş dikte aracı — **Linux / Wayland** için, **tamamen offline**.

Tuşu basılı tut, konuş, bırak: ses yazıya çevrilir ve imlecin o an nerede
olduğuna bakılmaksızın oraya yapıştırılır — terminal, tarayıcı, editör, hepsi.
Ağ çağrısı yok; whisper.cpp modeli senin makinende çalışır.

Wispr Flow tarzı basılı-tut-konuş dikte akışından esinlenildi; Linux/Wayland
için sıfırdan yazıldı, başka bir projenin kodu kullanılmadı.

**Gerekenler:** Wayland oturumu (KDE / GNOME / Hyprland — compositor fark etmez),
Arch tabanlı bir dağıtım (`setup.sh` `pacman` kullanıyor).
**NVIDIA GPU opsiyonel** — varsa CUDA + `large-v3-turbo`, yoksa sessizce
CPU + `small` modeline düşer.

```bash
git clone https://github.com/erkinozturk/VibeDictate.git
cd VibeDictate
./setup.sh all        # paketler → izinler → model → derleme → servis
# input grubu için MAKİNEYİ YENİDEN BAŞLAT (logout yetmiyor), sonra:
vibedictate --doctor
systemctl --user enable --now vibedictate.service
```

Varsayılan hotkey **sağ Ctrl**. Varsayılan dil **otomatik algılama**
(`language: "auto"`); Türkçe'ye sabitlemek istersen `config.json > language`
değerini `"tr"` yap — whisper dili her seferinde tahmin etmek zorunda kalmaz.

---

## Kullanım

| Hareket | Ne olur |
| --- | --- |
| Hotkey'i **basılı tut** | Basılı olduğu sürece kaydeder. Bırakınca: çöz → temizle → yapıştır. |
| Hotkey'e **çift tap** | **Eller serbest** oturum başlar — tuşu tutmadan kaydeder. |
| Eller serbestken **tek tap** | Oturumu bitirir ve işler. |
| Kayıt sırasında **Esc** | Kaydı atar, hiçbir şey yazılmaz. |
| İşlem sırasında **Esc** | Çalışan whisper prosesini öldürür. |
| _(unutulan oturum)_ | 5 dakika sonra kendiliğinden durur (`max_handsfree_seconds`). |

Tuşlar `evdev` ile **pasif** dinlenir — basılan tuş odaktaki uygulamaya da gider.
Wayland global kısayol vermediği için `/dev/input` seviyesinden okuyoruz; bu da
işi compositor'dan bağımsız kılıyor.

Sağ Ctrl nadiren kullanıldığı ve tek tuş olduğu için basılı-tut'a uygun.
Değiştirmek için `vibedictate --find-key` çalıştır, istediğin tuşa bas, çıkan
adı `config.json > hotkey` alanına yaz.

---

## Sistem tepsisi

Tepside bir mikrofon ikonu duruyor ve rengi durum makinesini birebir izliyor:

| İkon | Durum | Anlamı |
| --- | --- | --- |
| İçi boş **gri** mikrofon | `idle` | Boşta, mikrofon kapalı |
| Dolu **kırmızı** mikrofon | `recording` | Hotkey basılı, kayıt sürüyor |
| Dolu **yeşil** mikrofon + nokta | `handsfree` | **Eller serbest oturum — mikrofon açık** |
| Dolu **turuncu** mikrofon | `processing` | Çözümleme sürüyor |

Asıl amaç yeşil olan: eller serbest oturumda tuşa basmadığın için mikrofonun
açık kaldığını fark etmeyebiliyorsun. Yanındaki nokta, rengi görmeyen
temalarda ve renk körlüğünde de bu durumu ayırıyor. Animasyon/yanıp sönme yok —
bazı tepsi uygulamalarında ikon güncellemesi tamponlanıp titriyor, sabit renk
her yerde aynı görünüyor. Üstüne gelince ipucu metni de durumu yazıyor.

Sağ tık menüsü: **Open settings** (`xdg-open` ile `config.json`), **Reload
settings**, **Show logs**, **Restart**, **Quit**.

**Reload settings** whisper modelini, LLM ayarlarını, yapıştırma
yöntemini ve bildirim ayarlarını uygulamayı kapatmadan uygular. `hotkey` /
`cancel_key` istisna: bunlar açılışta evdev dinleyicisine bağlandığı için
yeniden başlatma gerekiyor, değiştiyse bildirim bunu söylüyor. Yeni ayar
bozuksa (ör. model dosyası yok) hiçbir şey değişmez, çalışan kurulum ayakta
kalır.

**Restart** `os.execv` kullanıyor: PID değişmediği için systemd bunu bir
çıkış saymıyor, `Restart=` politikası devreye girmiyor ve servis restart
döngüsüne girmiyor.

Tepsi opsiyonel: `config.json > tray.enabled` `false` ise ya da `python-pyqt6` /
`python-qasync` kurulu değilse program tepsisiz çalışır — hata vermez, logda
bir satır yazar.

---

## Kurulum

`./setup.sh all` tek komutta her şeyi yapar. Adımları ayrı ayrı da
çalıştırabilirsin — sıra önemli:

```bash
./setup.sh deps       # paketler (GPU varsa cmake/git, yoksa whisper-cpp)
./setup.sh perms      # input grubu + /dev/uinput udev kuralı
./setup.sh model      # whisper modeli (boyut verilmezse GPU'ya göre seçilir)
./setup.sh build      # GPU varsa whisper.cpp'yi CUDA ile derler
./setup.sh service    # ydotoold + vibedictate systemd user servisleri
```

`model` adımına boyutu elle de verebilirsin:
`./setup.sh model small|medium|large-v3-turbo`

### GPU varsa ne oluyor

`nvcc` **ve** çalışan bir NVIDIA sürücüsü varsa `build` adımı whisper.cpp'yi
`~/.local/share/vibedictate/whisper.cpp` altına klonlayıp CUDA ile derliyor:

* compute capability `nvidia-smi --query-gpu=compute_cap` ile otomatik
  bulunuyor (ör. RTX 3050 → `-DCMAKE_CUDA_ARCHITECTURES=86`),
* host compiler `$NVCC_CCBIN`'den alınıyor (aşağıdaki tuzaklara bak),
* model `large-v3-turbo` indiriliyor,
* `config.json > whisper.bin` ve `whisper.model` buna göre yazılıyor.

Derleme birkaç dakika sürüyor ve bir kere yapılıyor; binary yerindeyse
sonraki çalıştırmalar atlıyor (`rm -rf ~/.local/share/vibedictate/whisper.cpp/build`
ile zorlayabilirsin).

GPU yoksa `build` sessizce geçiyor, `deps` adımında kurulan `whisper-cpp`
paketinin `whisper-cli`'ı PATH'ten kullanılıyor ve model `small` oluyor.

### Kontrol

```bash
vibedictate --doctor
```

Her satır tek tek kontrol edilir; eksik olanın altında ne yapılacağı yazar:

```
vibedictate installation check

  [OK  ] 'input' group membership
  [OK  ] 'input' group active in this session
  [OK  ] /dev/input readable
  [OK  ] recording tool (parecord/arecord)
  [OK  ] whisper-cli
  [OK  ] wl-clipboard
  [OK  ] ydotool
  [OK  ] ydotoold running
  [OK  ] model file (ggml-large-v3-turbo.bin)
  [OK  ] tray icon (python-pyqt6 + python-qasync)
  [OK  ] service bound to graphical-session.target

Everything is ready.
```

Eksik olan satırlar `[MISS]` ile işaretlenir ve altına ne yapılacağı yazılır.

Her şey OK ise başlat:

```bash
systemctl --user enable --now vibedictate.service
journalctl --user -u vibedictate -f
```

Önce elle denemek istersen: `./vibedictate.py`

---

## Model seçimi

Türkçe için çok dilli model şart (`.en` sonekli olanlar sadece İngilizce).

| Model | Boyut | CPU (Ryzen 5 5500) | Kalite |
| --- | --- | --- | --- |
| `small` | ~466 MB | 10 sn ses ≈ 3-5 sn | Günlük dikte için yeterli |
| `medium` | ~1.5 GB | 10 sn ses ≈ 10-15 sn | Belirgin daha iyi |
| `large-v3-turbo` | ~1.6 GB | CPU'da yavaş | En iyi; CUDA ile gerçek zamandan hızlı |

---

## Ayarlar

`~/.config/vibedictate/config.json` — ilk çalıştırmada varsayılanlarla oluşur.
Değişiklikten sonra tepsiden **Reload settings**, ya da
`systemctl --user restart vibedictate`.

Depodaki iki örnek dosya:

* `config.example.json` — tüm anahtarları içeren şablon (CPU / `small`)
* `config.cuda-example.json` — CUDA + `large-v3-turbo` kurulumu

**Örnek dosyalardaki yolları kendinize göre düzeltin.**
`config.cuda-example.json` içindeki `/home/USER/...` yolunu kendi kullanıcı
adınızla değiştirin (ya da `./setup.sh build` çalıştırın; o zaten doğru yolu
`config.json`'a kendisi yazar).

| Anahtar | Ne işe yarar |
| --- | --- |
| `hotkey` / `cancel_key` | evdev tuş adları (`--find-key` ile bul) |
| `tap_threshold_ms` | Bunun altında basılı kalma "tap" sayılır |
| `double_tap_window_ms` | İki tap arası bu süreden kısaysa eller serbest başlar |
| `max_handsfree_seconds` | Unutulan oturumun kendini kapatma süresi |
| `min_recording_ms` | Bundan kısa kayıtlar işlenmeden atılır |
| `stop_tail_ms` | Tuş bırakıldıktan sonra kaydın devam ettiği süre |
| `language` | Whisper dili; `"auto"` da olur |
| `whisper.bin` | `whisper-cli` yolu; boşsa PATH'ten bulunur |
| `whisper.model` | `ggml-*.bin` model dosyası |
| `whisper.threads` | `0` = CPU çekirdek sayısı − 1 |
| `whisper.extra_args` | whisper-cli'a eklenen bayraklar (ör. `["-bs", "5"]`) |
| `llm.*` | Opsiyonel yerel Ollama temizliği (aşağıda) |
| `output.method` | `"paste"` (pano + Ctrl+V) veya `"type"` (tuş tuş yaz) |
| `output.paste_keycodes` | Varsayılan `[29, 47]` = Ctrl+V |
| `notify` / `notify_progress` | Bildirimlerin tamamı / sadece ara durumlar |
| `sound`, `keep_wav` | Ses efekti; kayıt WAV'ını `/tmp`'de bırak (hata ayıklama) |
| `tray.enabled` | Tepsi ikonu |

### Yapıştırma tuşu

Varsayılan `Ctrl+V` (`[29, 47]`). Terminalde `Ctrl+Shift+V` gerekiyorsa
`[29, 42, 47]` yap (42 = LEFTSHIFT).

### Kayıt kuyruğu (`stop_tail_ms`)

Hotkey bırakıldığında kayıt prosesi hemen öldürülmez, `stop_tail_ms` kadar
(varsayılan 300 ms) daha okunur. Ses aygıtından gelen veri her zaman gerçek
zamanın ~100-175 ms gerisinde olduğu için anında kapatmak cümlenin son hecesini
yiyor. Bekleme kayıt işçisinde yapılır, tuş dinleme döngüsünü bloklamaz.
`0` yaparsan davranış eski haline döner.

### Bildirimler (`notify_progress`)

`notify` bildirimlerin tamamını açıp kapatıyor. `notify_progress` ise sadece
ara durum bildirimlerini ("Recording…", "Transcribing…") kapatıyor; sonuç
bildirimi, hatalar ve iptal mesajları gelmeye devam ediyor. Tepsi ikonu zaten
durumu gösterdiği için `false` mantıklı bir tercih.

---

## LLM temizliği (opsiyonel, yerel)

```json
"llm": { "enabled": true, "model": "qwen2.5:3b" }
```

Ollama'da küçük ve hızlı bir model yeterli — iş sadece dolgu kelime temizliği
ve noktalama. Kod modeli kullanma; `qwen2.5:3b` veya `llama3.2:3b` daha uygun.

Varsayılan prompt İngilizce ama **dil-bağımsız**: modele metnin kendi dilini
koruması söyleniyor, herhangi bir dile ait dolgu kelime listesi gömülü değil.
Türkçe dikte de İngilizce dikte de aynı promptla temizleniyor.

```bash
ollama pull qwen2.5:3b
```

Model saçmalarsa ya da istek başarısız olursa ham transkript olduğu gibi
kullanılır — araç asla kelimelerini yemez. İstek `127.0.0.1:11434`'e gider,
makineden çıkmaz.

---

## Bilinen tuzaklar

Bunların hepsi kurulum sırasında canımızı yaktı; hata mesajı vermeden
yanlış çalışan şeyler oldukları için ayrı bir bölümü hak ediyorlar.

**`input` grubu için logout yetmiyor, makineyi yeniden başlatman gerekiyor.**
Grup üyeliği `/etc/group`'a yazılıyor ve yeni bir giriş oturumunda görünüyor,
ama servisi çalıştıran systemd **kullanıcı yöneticisi** logout/login'de
ölmüyor: eski grup listesiyle çalışmaya devam ediyor, dolayısıyla vibedictate
`/dev/input`'u hâlâ açamıyor. `--doctor` bunu iki ayrı satırda gösteriyor:
`'input' group membership` OK ama `'input' group active in this session`
`[MISS]` olur.

**Arch'ta paket adı `whisper-cpp`, `whisper.cpp` değil.** Nokta değil tire, ve
resmi `extra` deposunda (AUR'da aramaya gerek yok). CachyOS'ta x86_64_v3
optimize build geliyor.

**`/opt/cuda/bin` içinde `gcc`/`g++` symlink'i yok.** nvcc her gcc sürümünü
kabul etmiyor ve "cuda paketi kendi derleyicisini getirir" varsayımı Arch'ta
tutmuyor. Host compiler'ı açıkça vermek gerekiyor; `setup.sh` bunu
`$NVCC_CCBIN`'den okuyor:

```bash
NVCC_CCBIN=/usr/bin/g++-15 ./setup.sh build
```

Değişken boşsa script `/opt/cuda/bin/g++` ve `g++-15/14/13`'e bakıp
bulamazsa cmake varsayılanıyla devam ediyor — `unsupported GNU version`
hatası alırsan sebebi bu.

**`parecord --latency-msec=50` olmadan kısa kayıtlar sessizce boş çıkıyor.**
Varsayılan tamponla `parecord` ilk ~2 saniye boyunca stdout'a hiçbir şey
yazmıyor; 2 saniyeden kısa diktelerde elimize sıfır bayt geçiyor, whisper de
boş metin döndürüyor. Hata yok, uyarı yok, sadece hiçbir şey yapışmıyor.
`pw-record` ve `arecord`'da bu sorun yok.

**systemd unit'i `graphical-session.target`'a bağlı olmalı.**
`WantedBy=default.target` ile servis grafik oturumdan **önce** başlıyor ve
`WAYLAND_DISPLAY` / `DISPLAY` değişkenleri ortamında hiç olmuyor. Sonuç:
tepsi ikonu hiç görünmüyor ve `wl-copy` çalışmıyor — ikisi de görünür bir
hata vermeden. Doğrusu `After=` + `PartOf=` + `WantedBy=graphical-session.target`;
`setup.sh service` bunu yazıyor ve eski `default.target` symlink'ini de
taşıyor. `--doctor` unit dosyasına bakıp uyarıyor.

**Çalışan kopya ile geliştirme kopyası ayrı.** Servis
`~/.local/bin/vibedictate`'i çalıştırıyor; depodaki `vibedictate.py` sadece
kaynak. Kodu değiştirdiğinde kurulumu tazelemen gerekiyor:

```bash
./setup.sh service && systemctl --user restart vibedictate.service
```

---

## Sorun giderme

**"No keyboard found"** → `input` grubunda değilsin ya da makineyi yeniden
başlatmadın. `vibedictate --doctor` hangisi olduğunu söylüyor.

**Metin panoya geliyor ama yapışmıyor** → `ydotoold` çalışmıyor:
`systemctl --user status ydotoold`. Hâlâ olmuyorsa `/dev/uinput` iznine bak,
`ls -l /dev/uinput` çıktısında grup `input` olmalı.

**Hiçbir şey yapışmıyor, pano da boş** → servis grafik oturumdan önce başlamış
olabilir; yukarıdaki `graphical-session.target` tuzağına bak.

**Transkript boş çıkıyor** → önce yanlış mikrofon kaynağı seçili mi diye bak.
`config.json > keep_wav: true` yapıp `/tmp/vibedictate-*.wav` dosyasını dinle.
Kaydın kendisi boş/kısaysa `parecord --latency-msec` tuzağı devrede.

**Sağ Ctrl başka bir şeyi tetikliyor** → `vibedictate --find-key` ile başka bir
tuş seç.

**Tepsi ikonu görünmüyor** → `vibedictate --doctor`. En sık iki sebep:
`python-pyqt6`/`python-qasync` kurulu değil, ya da yine
`graphical-session.target` meselesi:

```bash
./setup.sh service
systemctl --user restart vibedictate.service
systemctl --user show -p Environment vibedictate.service   # kontrol
```

**CUDA derlemesi `unsupported GNU version` diyor** →
`NVCC_CCBIN=/usr/bin/g++-15 ./setup.sh build` (sürüm numarasını nvcc'nin
kabul ettiği bir gcc ile değiştir).

**Dikte yavaş** → `vibedictate --doctor` çıktısındaki model adına bak. GPU'n
varsa `large-v3-turbo` + CUDA build kullanıyor olmalısın; CPU'da o model
gerçek zamandan yavaş, `small`'a düş.

Loglar: `~/.local/share/vibedictate/vibedictate.log`
ya da `journalctl --user -u vibedictate -f`

---

## Neler var

```
vibedictate.py          tek dosyalık daemon
  Recorder              parecord/pw-record/arecord -> ham 16 kHz mono PCM
  Transcriber           whisper-cli alt prosesi, iptal edilebilir
  LocalCleaner          Ollama /api/generate, hata olursa ham metne döner
  TextInserter          wl-copy + ydotool Ctrl+V (ya da doğrudan yazma)
  DictationController   kaydet -> çöz -> temizle -> yapıştır durum makinesi
  TrayIcon              QSystemTrayIcon, durumu renkle gösterir (opsiyonel)
  watch()               evdev pasif klavye dinleme
setup.sh                paketler, izinler, model, CUDA derlemesi, systemd
config.example.json     ayar şablonu (CPU / small)
config.cuda-example.json  CUDA + large-v3-turbo örneği
README.md               İngilizce dokümantasyon
README.tr.md            bu dosya
```

Ağ kodu yok: `urllib` sadece `127.0.0.1:11434`'e gider, o da `llm.enabled`
açıksa.

Tepsi açıkken Qt döngüsü asıl döngü oluyor ve `qasync` asyncio'yu ona bağlıyor
— tek thread, periyodik "pump" yok. Tepsi kapalıyken düz asyncio döngüsü
çalışıyor, Qt hiç import edilmiyor.

---

## Lisans ve bakım

MIT — bkz. [LICENSE](LICENSE).

Bu kişisel bir araçtır. Kendi makinemde çalışsın diye yazıldı ve öyle
paylaşılıyor; aktif bakım sözü verilmiyor, issue'lar ve PR'lar
cevaplanmayabilir. Fork'layıp kendine göre değiştirmen en sağlıklısı.
