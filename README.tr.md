# Yaver

[English](README.md) · **Türkçe**

**Sürüm:** kökteki [`VERSION`](VERSION) dosyasına bakın.

**Yaver** Jira, GitLab veya Azure DevOps Server’dan gelen işi **OpenCode** ajanlarıyla (OpenCoderman **derman-plan** / **derman-build** / **derman-test**) çalıştıran bir Python servisidir. Depoyu izole bir geçici klasöre klonlar, ilerlemeyi işe veya inceleme konusuna yazar, başarılı bir yapımda dalı iter ve birleştirme / çekme isteği açar.

İşin başlama yolları:

| Giriş | Nasıl başlar | Kabulden sonra | İş bitince |
|--------|--------------|----------------|------------|
| **Jira panosu** | Tarayıcı: Yapılacaklar benzeri sütun + bota atama | Pano → **Devam Ediyor** (In Progress) | Orada kalır. Yeniden çalıştırmak için **Yapılacaklar**’a alın. |
| **GitLab MR yorumu** | Birleştirme isteğinde `@bot /yaver …` | Pano değişmez | Aynı konuda yanıt. Birleşen / kapanan MR geçici kopyayı siler. |
| **Azure PR yorumu** | Çekme isteğinde `@bot /yaver …` | Pano değişmez | Aynı konuda yanıt. Tamamlanan / vazgeçilen PR geçici kopyayı siler. |
| **Azure Boards iş öğesi** | Botu **Yapılacaklar** / **Devam Ediyor** (veya New / Active / Doing …) üzerinde atayın | Durum → **InProgress** karşılığı (**Active** / **Doing** / **Committed** / **In Progress**) | Orada kalır. Yaver öğeyi **Resolved** veya **Done** yapmaz. |

Aynı `Repository` + `Source branch` + `Target branch` + tür (`plan` / `build`) mevcut OpenCode oturumunu sürdürür. Paralellik `MAX_CONCURRENT_JOBS` ile sınırlıdır.

---

## Ne yapar

1. İşi **bulur** (Jira tarayıcısı, GitLab webhook, Azure webhook).
2. İşteki `{params}` bloğundan **yönlendirir** (`Mode: plan`, `Mode: build`, `Mode: test`; varsayılan build).
3. OpenCode’u geçici klonda **çalıştırır**.
4. Plan, ilerleme, hata ve bitişi işe veya inceleme konusuna **yazar**.
5. Yapım başarılıysa dalı **iter** ve MR/PR **açar** (itme ve MR orkestratöre aittir; model itmemelidir).
6. Aynı süreçte bir **işlem panosu** sunar (görevler, tarama, depolama, güvenli ayarlar).

---

## Mimari

```text
 Jira pano tarayıcısı       GitLab proje kancası        Azure servis kancası
 Yapılacaklar + bot         MR’de @bot /yaver           PR’de @bot /yaver
                                                        iş öğesini ata
              \                    |                    /
               \                   |                   /
                ▼                  ▼                  ▼
        ┌──────────────── Yaver (tek süreç) ───────────────────┐
        │  Giriş  →  İş işlemcisi  →  OpenCode serve (--dir)   │
        │     {params} ile geçici klon                         │
        │     Jira / MR / PR / iş öğesi yorumları              │
        │     git push + GitLab MR veya Azure PR (yapım)       │
        │  İşlem panosu  ·  takılı iş izleyici  ·  JSON durum  │
        └──────────────────────────────────────────────────────┘
```

---

## İş nasıl başlar (tüm girişler)

Her işte Yaver’ın uzak depoyu ve dalları bilmesi için bir **`{params}`** bloğu gerekir (bkz. [İş şablonu](#iş-şablonu-params)). MR/PR üzerindeki `/yaver` işleri, bağlı işte `{params}` yoksa o MR/PR’nin depo ve dallarını kullanabilir.

### Kabulden sonra / iş bitince

| | Jira | Azure iş öğesi | GitLab MR / Azure PR |
|--|------|----------------|----------------------|
| **Kabul** | **In Progress**’e geçiş (elinden geldiğince) ve PAT kullanıcısına atama | `System.State` o tipin **InProgress** adına: Agile/CMMI **Active**, Basic **Doing**, Scrum **Committed** veya **In Progress** | Pano değişmez |
| **İş bitti** | **In Progress**’te kalır. Yalnızca yorum. | **Active / Doing / In Progress**’te kalır. **Resolved / Done olmaz.** | Konuda yanıt |
| **Yeniden çalıştır** | İşı **Yapılacaklar**’a alın (botta kalsın) | Yapılacaklar / Devam Ediyor (veya New / Active / Doing) üzerinde yeniden atayın. Hâlâ atalıyken Active → New **yeniden kuyruğa almaz**. Hata sonrası başlık/açıklamayı düzenleyin. | Yeni `@bot /yaver …` yorumu |
| **Devam eden iş** | Tarama veya webhook gürültüsüyle yeniden başlatılmaz | Aynı | Aynı |

---

## 1. Jira panosu (tarayıcı)

Jira’da tek giriş budur. Yorumla iş başlatan bir Jira webhook’u yoktur.

### Kabul

Hepsi birden:

- İş, ayarlı **panoda** (`JIRA_BOARD_ID`). Scrum: **yalnızca ilk aktif sprint**.
- Durum **Yapılacaklar** gibi görünür (ad veya `statusCategory` new: To Do, New, Open, Backlog, Yapılacaklar, …).
- Atanan `JIRA_TRIGGER_USER` ile eşleşir.
- `JIRA_TRIGGER_LABEL` doluysa işte o etiketlerden biri de gerekir.
- Zaten `planning` / `executing` değildir.

**Yapılacaklar + bota atama = yeniden iş (bilinçli).** `completed` / `error` / `cancelled` sonrası işi Yapılacaklar’da bırakmak (veya oraya geri almak) yeni bir koşu başlatır. Kabulden sonra Yaver panoyu **In Progress**’e alır; bir sonraki tarama, siz yine Yapılacaklar’a alana kadar ikinci işi açmaz.

`plan_ready` istisnadır: **`Mode: plan` kendi başına kod yazmaz.** Bkz. [Plandan sonra](#plandan-sonra).

### Örnek — aynı Jira işinde plan, sonra uygulama

1. Panoda `KAN-12` açın.

```text
Özet: Giriş hız sınırını planla

{params}
Repository: https://gitlab.example.com/group/app.git
Source branch: feature/KAN-12
Target branch: develop
Mode: plan
{params}
```

2. Bot’a atayın (`JIRA_TRIGGER_USER`, örn. `yaver`) ve **Yapılacaklar**’da bırakın.
3. Bir tarama aralığında Yaver:
   - işi kabul eder
   - **In Progress**’e alır
   - `{YAVER_DATA_DIR}/plans/KAN-12.md` yazar
   - planı **yorum** olarak ekler (açıklamaya yazmaz)
   - `plan_ready` etiketi ve yerel `plan_ready` durumu koyar
   - **durur**
4. **Aynı** işte uygulamak için iş **In Progress** iken etiketi `plan_ready` → `plan_execute` yapın.
5. Yaver bir **build** oturumu açar, plan dosyasını uygular, `feature/KAN-12` dalını iter, MR açar, bitiş yorumu yazar. Etiket `plan_executed` olur. Jira durumu **In Progress** kalır.
6. PR/MR’yi beğeninince işi Done’a siz alırsınız.

### Örnek — bitmiş Jira işini yeniden çalıştır

1. `KAN-12` `completed` ve hâlâ **In Progress**.
2. Açıklamayı düzeltin veya olduğu gibi bırakın.
3. İşi **Yapılacaklar**’a alın (botta kalsın).
4. Sonraki tarama yeni bir koşu kuyruğa alır.

### Örnek — hata, düzelt, dene

1. Kabul başarısız olur (bozuk `{params}`). Yaver yine de işi **In Progress**’e alır ve hata yorumu yazar.
2. Açıklamayı düzeltin.
3. **Yapılacaklar**’a alın. Sonraki tarama yeniden dener.  
   `error` sonrası Yapılacaklar’da kalıp metni değiştirmek de dener.

### Jira’da yapmayın

- Aynı plan işinde `Mode: plan`’ı `Mode: build` yaparak uygulamayı başlatmayın. Aynı işte uygulama yalnızca **`plan_execute` + In Progress**.
- Jira yorumlarında Azure `/planExecute` / `/planRefactor` kullanmayın.
- İş In Progress’te dururken ikinci bir koşu beklemeyin (`plan_execute` / `plan_refactor` dışında).

---

## 2. GitLab birleştirme isteği yorumları

**Proje** webhook’u kaydedin (Comments + Merge request):

`http://<yaver-host>:8080/yaver/webhook/gitlab`

`GITLAB_WEBHOOK_SECRET` GitLab’ın gönderdiği sırla aynı olsun. `GITLAB_TRIGGER_USER` (virgülle kullanıcı adları, `@` yok).

### Komutlar

| Yorum | Ne olur |
|-------|---------|
| `@yaver /yaver login için test ekle` | İş başlar. İstem yorumun geri kalanıdır. |
| `@yaver` (`/yaver` yok) | **O konuda** kullanım notu. İş yok. |
| `@yaver /ask …` veya `@yaver /review …` | Yok sayılır (başka ajan). Kullanım notu yok. |

### İş hangi kayda bağlanır

Sıra:

1. MR başlığında `JIRA_PROJECTS` ile eşleşen Jira anahtarı (`feat(KAN-12): …`)
2. Başlıkta `WIT-…`
3. Bu koleksiyonda duran `#42` (Azure iş öğesi)
4. `Closes KAN-12` benzeri satır
5. Açıklamada `WIT-…` veya `#id`
6. Aynı depo + kaynak + hedef ile yerel iş
7. Yedek anahtar `GL-{PROJE}-{iid}`

### Örnek

MR başlığı: `feat(KAN-12): giriş hız sınırı`

```text
@yaver /yaver yeni sınırlayıcıyı birim testleriyle kapsa
```

Yaver ev sahibi PAT ile klonlar, o depo + dallar için **build** oturumu varsa onu sürdürür, aynı tartışmada yanıtlar, ajan commitlediyse MR’yi açar veya günceller.

MR **birleşince** veya **kapanınca** eşleşen geçici kopya silinir.

---

## 3. Azure DevOps çekme isteği yorumları

GitLab ile aynı fikir. `AZURE_WEBHOOK_ENABLED=true`. Projede Service hooks → Web Hooks:

- Olaylar: **Pull request commented**, **updated**, **merged**, **abandoned**
- Adres: `http://<yaver-host>:8080/yaver/webhook/azure`
- Webhook sırrı yok

`AZURE_TRIGGER_USER` ve `AZURE_COLLECTION_PATS` (koleksiyon URL → PAT). Klon / itme / PR, HTTP Basic `pat:<PAT>` kullanır.

### Komutlar (yalnızca PR konusu)

| Yorum | Ne olur |
|-------|---------|
| `@yaver /yaver bu farkı anlat` | PR üzerinde iş başlar |
| `@yaver` (`/yaver` yok) | O konuda kullanım notu |
| `@yaver /ask …` | Yok sayılır |

PR’de `/planExecute` / `/planRefactor` **kullanmayın**. Bunlar yalnızca iş öğesi yorumlarıdır.

### İş hangi kayda bağlanır

GitLab ile aynı sıra: Jira başlığı, `WIT-…`, bu koleksiyonda `#42`, Closes, açıklama, depo/kaynak/hedef, sonra `AZ-{PROJE}-{id}`.

Panoda iş öğesinin yerel adı `WIT-BETA-42`’dir. Ajanın **Ticket** / `{ISSUE_KEY}` satırı sayısal kimliktir: `42`.

### Örnek

PR başlığı: `feat(KAN-12): giriş sınırlayıcı` **veya** `Fix #42`

```text
@yaver /yaver 40. satırdaki null kontrolünü sıkılaştır
```

Tamamlanan veya vazgeçilen PR eşleşen geçici kopyayı siler. Bağlı MR/PR’si olmayan klasörler Depolama’da uyarılır (otomatik silinmez).

---

## 4. Azure Boards iş öğeleri (webhook)

PR ile aynı Azure adresi. **Work item created**, **updated**, **commented** kancalarını ekleyin. Updated yalnızca **Assigned To** için kullanılır. Yorumlar commented kancasındadır. Durum, açıklama ve etiket değişiklikleri iş başlatmaz.

### Kabul

- Assigned To `AZURE_TRIGGER_USER` ile eşleşir
- Durum **To Do** veya **In Progress**, ya da aynı süreç şablonu sütunu:

| Sütun türü | İşi başlatan adlar |
|------------|-------------------|
| Yapılacaklar | **To Do**, **New**, **Proposed**, **Approved**, Open, Backlog, Yapılacak / Yapılacaklar |
| Devam Ediyor | **In Progress**, **Active**, **Doing**, **Committed**, WIP, Devam Ediyor |
| Alınmaz | **Resolved**, **Done**, Closed, Completed, Removed |

Kabulden sonra Yaver:

1. Durumu o tipin **In Progress** adına çeker (Agile **Active**, Basic **Doing**, Scrum **Committed** veya **In Progress**)
2. Koleksiyon PAT kullanıcısına atar
3. `{params}` ile işi başlatır

**Jira’dan farklı:** Hâlâ atalıyken Active → New (veya In Progress → To Do) **yeniden kuyruğa almaz**. Yalnızca ilk görülme. `error` sonrası öğeyi yeniden atayın. Tahta taşıma, açıklama ve etiket düzenlemek yeniden denemez. Plandan sonra aşağıdaki yorumları kullanın.

Yaver iş öğesini **asla** Resolved veya Done yapmaz.

### Plandan sonra (yalnızca Azure iş öğesi)

Azure’da Jira etiketleri `plan_ready` / `plan_execute` **kullanmayın**.

| İş öğesi yorumu | Ne olur |
|-----------------|---------|
| `@yaver /planExecute` | Bekleyen planı uygular (öğe zaten `plan_ready` olmalı) |
| `@yaver /planRefactor API’yi sıkılaştır` | Planı **plan** oturumunda revize eder, yine `plan_ready` |
| Bu komutlar olmadan `@yaver` | İş öğesi kullanım notu (PR’ye yazılmaz) |

### Örnek — New bir hatayı ata, sonra uygula

1. Beta projesinde iş öğesi **42**, durum **New** (veya To Do / Active / Doing).

```text
{params}
Repository: https://tfs.example.com/tfs/DefaultCollection/Beta/_git/app
Source branch: feature/42
Target branch: develop
Mode: plan
{params}
```

2. `yaver`’a atayın.
3. Yaver kabul eder, **Active** (Agile) / **Doing** (Basic) / **In Progress** yapar, planı yazar, yorumlar, yerel `plan_ready` koyar, **durur**. Pano In Progress benzeri kalır.
4. Yorum:

```text
@yaver /planExecute
```

5. Yaver uygular, iter, PR açar. İş öğesi **Active / In Progress** kalır. PR bitince kapatmayı siz yaparsınız.

### Örnek — doğrudan yapım (plansız)

`{params}` içinde `Mode: build`, New/To Do/Active üzerinde atayın. Tek build oturumu; `/planExecute` gerekmez.

---

## Plandan sonra

```text
Jira
  Yapılacaklar + bot  →  Mode: plan  →  plan_ready + etiket plan_ready  →  In Progress, dur
       ├─ plan_ready → plan_execute (hâlâ In Progress)     →  yapım
       ├─ plan_ready’i kaldır, plan_refactor ekle, @bot    →  planı revize et
       └─ yeni iş, Mode: build, aynı depo/dallar           →  ayrı build oturumu

Azure iş öğesi
  To Do / In Progress (veya New / Active / Doing) ata  →  plan_ready, dur
       ├─ yorum @bot /planExecute                        →  yapım
       ├─ yorum @bot /planRefactor <istem>               →  planı revize et
       └─ yeni iş öğesi, Mode: build, aynı depo/dallar   →  ayrı build oturumu
```

Aynı depo + kaynak + hedef için plan ve yapım **ayrı** OpenCode oturumlarıdır.

Paneldeki **Start** kapalıdır. `/planExecute`’u Jira, GitLab veya Azure PR yorumunda kullanmayın.

---

## İş şablonu (`{params}`)

Jira açıklamasına veya Azure iş öğesi açıklamasına koyun:

```text
{params}
Repository: https://gitlab.example.com/group/your-repo
Source branch: feature/PROJ-123
Target branch: develop
Mode: plan
{params}
```

| Alan | Anlamı |
|------|--------|
| **Repository** | Klon adresi. Azure `…/_git/…` adreslerinin sonuna `.git` eklemeyin. |
| **Source branch** | İş / MR kaynak dalı. Yoksa veya `main`/`develop` ise dal `feature/{ISSUE_KEY}` olur |
| **Target branch** | Uzakta var olmalıdır; iş bunun üzerine kurulur; MR/PR **buraya** birleşir |
| **Mode** | **`plan`** — yalnız plan, itme yok. **`build`** — uygula, it, MR/PR aç. **`test`** — yalnız birim testleri |

Eksik şablonlarda kullanıcıya biçim yardımı yorumu düşer.

---

## Görev durumları

```text
pending → planning | executing → (plan_ready) → completed | error | cancelled
```

| Durum | Anlamı |
|-------|--------|
| `planning` / `executing` | Ajan çalışıyor — giriş gürültüsüyle yeniden başlatılmaz |
| `plan_ready` | Plan bitti; hata değil. Jira: `plan_execute`. Azure iş öğesi: `/planExecute` |
| `completed` | Teslim edildi. Jira: yeniden iş için Yapılacaklar. Azure WIT: otomatik kuyruk yok |
| `error` | Battı; yorum nedeni yazar. Jira: Yapılacaklar veya metni düzenle. Azure WIT: metni düzenle |
| `cancelled` | Operatör iptali. Jira Yapılacaklar + bot yine yeniden iştir |

---

## Hızlı kurulum

### Linux

Ayrıntı: [packaging/linux/README.md](packaging/linux/README.md).

```bash
git submodule update --init --recursive
./install-dashboard.sh
./install-backends.sh
./install-codex.sh
# .env — en az JIRA_HOST, JIRA_API_TOKEN, JIRA_BOARD_ID
./start-backend.sh
```

İşlem panosu: **http://127.0.0.1:8080**  
OpenCode TUI: proje klasöründen `./start-opencode.sh` (`$HOME`’dan değil).

### Windows (çevrimdışı zip)

Ayrıntı: [packaging/windows/README.md](packaging/windows/README.md).

```cmd
install-dashboard.bat
install-backends.bat
install-codex.bat
start.bat
```

TUI’yi yalnızca proje klasöründen **`start-opencode.bat`** ile açın.

### Bağımsız çalıştırılabilirler

CI **Standalone Executables** `yaver` / `yaver.exe` üretir. Linux’ta ev sahibine uyan Ubuntu 18.04 / 20.04 / 22.04 / 24.04 paketini indirin. OpenCode / Codex ikilinin içinde değildir. Bkz. [packaging/pyinstaller/](packaging/pyinstaller/README.md).

---

## İşlem panosu

Daemon ile açılır (`DASHBOARD_ENABLED=true`). Çevrimdışı zip varsayılanı `0.0.0.0` (LAN). Yalnızca döngü: `DASHBOARD_HOST=127.0.0.1`.

| | |
|--|--|
| Adres | `http://127.0.0.1:8080` |
| Giriş | İsteğe bağlı `DASHBOARD_USERNAME` + `DASHBOARD_PASSWORD`. Boş = giriş yok. Jira tarayıcısına ve GitLab/Azure webhook’larına **uygulanmaz**. |

**Ön yüz yalnızca gösterir.** Süzme ve tarama hesabı arka uçtadır.

- **Tasks / Jobs** — canlı ve geçmiş koşular (istem ve günlükler seçili işe özgüdür)
- **Poll** — son Jira pano anlığı
- **Storage** — geçici klonlar. İş klonu sahipse silme reddedilir. Birleşen GitLab MR ve biten Azure PR eşleşen klasörü siler. Bağlı MR/PR’si olmayanlar uyarılır.
- **Scheduled** — sonra Jira işi veya Azure iş öğesi oluşturun / var olanı bulun. **Cancel** yalnız `scheduled` / `error` içindir (`dispatching` iptal edilemez).
- **Settings** — pano, tarama aralığı, tetik adları, Azure koleksiyon PAT’leri (jeton gösterilmez)

**Start** kapalıdır. **Cancel** ajan çocuklarını hemen öldürür.

---

## Yapılandırma

[`.env.example`](.env.example) → `.env`. Gizlileri commit etmeyin.

### Jira

| Değişken | Açıklama |
|----------|----------|
| `JIRA_HOST` | Temel URL |
| `JIRA_API_TOKEN` | Yerinde PAT veya Cloud API jetonu |
| `JIRA_EMAIL` | Yalnız Cloud/dev → HTTP Basic. Boş = Bearer PAT |
| `JIRA_PROJECTS` | Proje anahtarları; GitLab/Azure başlığından `KAN-12` okumak için de kullanılır |
| `JIRA_BOARD_ID` | Taranacak Agile panosu (Jira keşfi için **zorunlu**) |
| `JIRA_TRIGGER_USER` | Atanan / bahis adları (virgül, `@` yok) |
| `JIRA_TRIGGER_LABEL` | İsteğe bağlı. Doluysa Yapılacaklar girişinde bu etiketlerden biri de gerekir |

### GitLab

| Değişken | Açıklama |
|----------|----------|
| `GITLAB_HOST_PATS` | JSON ev sahibi → PAT |
| `GITLAB_TRIGGER_USER` | `@ad /yaver` ile iş başlatan kullanıcılar |
| `GITLAB_WEBHOOK_SECRET` | `POST /yaver/webhook/gitlab` için zorunlu |

### Azure DevOps

| Değişken | Açıklama |
|----------|----------|
| `AZURE_COLLECTION_PATS` | JSON `https://host/tfs/Collection` veya `https://host/Collection` → PAT |
| `AZURE_WEBHOOK_ENABLED` | `/yaver/webhook/azure` üzerinde PR + iş öğesi (sır yok) |
| `AZURE_TRIGGER_USER` | PR `@ad /yaver` ve iş öğesi Assigned To |

### Ajan / yollar

| Değişken | Varsayılan | Açıklama |
|----------|------------|----------|
| `POLL_INTERVAL_SECONDS` | `30` | Jira pano taraması |
| `MAX_CONCURRENT_JOBS` | `6` | Paralel ajan işi |
| `DEFAULT_MODEL` | (`.env.example`) | OpenCode ve Codex ortak |
| `TEMP_DIR_BASE` | `C:\vd\t` / `/vd/t` | Geçici klonlar |
| `YAVER_DATA_DIR` | `C:\vd\yaver` / `/vd/yaver` | Durum, işler, oturumlar, planlar |

---

## CLI

```bash
python cli.py --help
python cli.py start
python cli.py process PROJ-123
python cli.py status
python cli.py show PROJ-123
python cli.py cancel PROJ-123
```

---

## Sorun giderme

| Belirti | Bakın |
|---------|--------|
| Jira tarayıcı boş | `JIRA_BOARD_ID`, Yapılacaklar, bot ataması, `python cli.py process KEY` |
| Jira Yapılacaklar + bot ama iş yok | `plan_ready` → `plan_execute`. To Do’da `completed`/`error`/`cancelled` **yeniden iştir** — günlüğe bakın |
| Azure atama işe yaramıyor | Durum To Do / In Progress / New / Active / Doing olmalı (Resolved/Done değil). Webhook açık mı? Assigned To `AZURE_TRIGGER_USER` ile uyuşuyor mu? |
| Azure planı uygulanmıyor | `@bot /planExecute` yorumunu **iş öğesine** yazın, PR’ye değil |
| MR/PR bahsi işe yaramıyor | `@bot /yaver …` gerekir. Yalnız bahis kullanım notu düşer |
| 401 / 403 Jira | Jeton; Cloud’da `JIRA_EMAIL` |
| Git / MR batıyor | `{params}` tam mı; `GITLAB_HOST_PATS` veya `AZURE_COLLECTION_PATS` |
| Panel yok | Daemon ayakta mı? `http://127.0.0.1:8080` |
| Windows TUI siyah ekran | Proje klasöründen `start-opencode.bat` |

---

## Güvenlik

1. **`.env`** git’e girmesin.
2. Panel girişi isteğe bağlıdır; ev sahibi güvenilir ağda değilse kilitleyin.
3. GitLab/Azure PAT yalnızca kayıtlı ev sahibi / koleksiyona gider.
4. En az yetkili ayrı bir bot hesabı kullanın.
5. Ham jetonları günlüğe yazmayın.

---

## İlgili belgeler

| Belge | Amaç |
|-------|------|
| [README.md](README.md) | İngilizce kılavuz |
| [AGENTS.md](AGENTS.md) | Katkı / ajan kuralları |
| [packaging/windows/README.md](packaging/windows/README.md) | Çevrimdışı Windows zip |
| [packaging/linux/README.md](packaging/linux/README.md) | Linux kurulum |
| [`.env.example`](.env.example) | Tam ortam şablonu |

---

## Lisans

MIT
