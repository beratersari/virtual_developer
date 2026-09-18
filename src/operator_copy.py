"""Turkish operator-facing text posted to Jira, Azure, and GitLab."""

from __future__ import annotations

from typing import Optional

HEADER_KIND_TR = {
    "Work started": "İş başladı",
    "Plan": "Plan",
    "Plan ready": "Plan hazır",
    "Progress": "İlerleme",
    "Done": "Tamamlandı",
    "Failed": "Başarısız",
    "Answer": "Yanıt",
}


def header_kind(kind: str) -> str:
    raw = (kind or "").strip()
    return HEADER_KIND_TR.get(raw, raw or "Güncelleme")


NO_SUMMARY = "(özet yok)"

ACK_HEADING = ""
ACK_INTRO = "Bu kayıt kabul edildi ve otomatik işlenecek."
ACK_ISSUE = "Kayıt"
ACK_WORKFLOW = "İş akışı"
ACK_STATUS = "Durum"
ACK_ANALYZING = "Gereksinimler inceleniyor"
ACK_BOARD = (
    "Yürütme başladığında pano durumu *Devam Ediyor* olur.\n"
    "İlerleme bu kayda yorum olarak yazılır."
)

PLAN_HEADING = ""
PLAN_INTRO = (
    "Bu kayıt için bir iş planı üretildi (plan kipi — *uzak depoya gönderim yok*)."
)
PLAN_LABEL = "Plan"
PLAN_FILE = "Plan dosyası"
PLAN_NEXT = "Sonraki adımlar"
PLAN_FOOTER = ""
PLAN_EMPTY = (
    "_Diskte plan içeriği bulunamadı. Planlama ajanı dosyayı yazmamış "
    "olabilir veya plan yolu çalışma alanıyla eşleşmiyor. İşe başlamadan "
    "ajan oturum günlüklerini kontrol edin._"
)

PLAN_NEXT_AZURE = (
    "* Planı bu yorumda inceleyin\n"
    "* *Güncellemek* için: `/planRefactor <ne değişecek>` yazıp beni etiketleyin\n"
    "* *Uygulamak* için: `/planExecute` yazıp beni etiketleyin\n"
    "* Veya *yeni* bir {{noformat}}Mode: build{{noformat}} kaydı açın "
    "(aynı Repository / dallar)\n"
    "* Gösterge panelinde Başlat düğmesi yok"
)

PLAN_NEXT_JIRA = (
    "* Planı bu yorumda inceleyin\n"
    "* *Güncellemek* için: {{noformat}}plan_ready{{noformat}} etiketini kaldırın, "
    "{{noformat}}plan_refactor{{noformat}} ekleyin ve beni etiketleyen bir yorum yazın\n"
    "* *Uygulamak* için: kayıt *Devam Ediyor* iken {{noformat}}plan_ready{{noformat}} "
    "etiketini {{noformat}}plan_execute{{noformat}} olarak yeniden adlandırın "
    "({{params}} içindeki Mode {{noformat}}plan{{noformat}} kalabilir)\n"
    "* Veya *yeni* bir {{noformat}}Mode: build{{noformat}} kaydı açın "
    "(aynı Repository / dallar)\n"
    "* Gösterge panelinde Başlat düğmesi yok"
)

PROGRESS_HEADING = ""
PROGRESS_EMPTY = "İlerleme güncellemesi (ayrıntı yok)."
PROGRESS_PCT = "İlerleme"

DONE_HEADING = ""
DONE_GENERIC = "İş bitti. Ayrıntılar için birleştirme isteğine / dala bakın."
DONE_CHANGES = "Yapılan değişiklikler"
DONE_DELIVERY = "Teslim"
DONE_NO_COMMITS = (
    "* Bu çalıştırmada yeni commit yok (ajan başarıyla bitti; "
    "mevcut dal/MR bu işe yeniden bağlanmadı)."
)
DONE_NOTE = "Not"
DONE_MR = "Birleştirme isteği"
DONE_BRANCH = "Özellik dalı"
DONE_NO_MR = (
    "* Birleştirme isteği adresi kaydedilmedi. Dal uzakta olabilir — "
    "GitLab’da {{noformat}}feature/{issue}{{noformat}} dalına bakın."
)
DONE_PUSH_NO_MR = (
    "* Dal gönderildi ama birleştirme isteği bağlantısı yok "
    "(glab eksik olabilir veya hedef dal yok)."
)
DONE_DURATION = "Süre"
DONE_SECONDS = "saniye"
DONE_SESSION = "Oturum"
DONE_AT = "Tamamlanma"
DONE_FOOTER = "Birleştirmeden veya kapatmadan önce gözden geçirin."

FAILED_HEADING = ""
FAILED_LEAD = "Bu kayıt işlenirken bir hata oluştu:"
FAILED_QUESTION_H = ""
FAILED_QUESTION = (
    "Ajan netleştirme sormak için durdu. Bu hizmet *gözetimsiz* çalışır "
    "(tek geçişli istem) — insan yanıt yolu yok. Mümkünse tek bir "
    "gözetimsiz devam denendi; oturum hâlâ eksik. Bu bir *çökme* değil."
)
FAILED_COMPACT_H = ""
FAILED_COMPACT = (
    "OpenCode oturumu yeni iş üretmeden otomatik sıkıştırmaya devam etti "
    "('Session auto-compacted' yineleniyor). Bu bir *çökme* veya "
    "*netleştirme sorusu* değil. Continue gönderilmedi — sıkıştırma ile "
    "yarışır ve bağlamı şişirir. İş zaman aşımını artırmak döngüyü kırmaz."
)
FAILED_INCOMPLETE_H = ""
FAILED_INCOMPLETE = (
    "OpenCode oturumu bağlam sıkıştırması veya tur ortası boşta kalınca durdu. "
    "Bu bir *çökme* değil — ajan bitirmeden sıkıştırma-devam bütçesini tüketti."
)
FAILED_TODOS_H = ""
FAILED_TODOS = (
    "Ajan kalan işi bitirmeden durdu (çoğu zaman gözetimsiz dürtmeden sonra "
    "açık yapılacaklar). Bu bir sıkıştırma bütçesi hatası veya çökme değil."
)
FAILED_LOCK_H = ""
FAILED_LOCK = (
    "Codex iş parçacığını sürdüremedi: başka bir süreç hâlâ yazıcıyı tutuyordu "
    "(`already has an active writer`). Bu eksik bir OpenCode oturumu değil. "
    "Orkestratör kısa süre yeniden dener, sonra mevcut dosyalardan yeni "
    "iş parçacığı açar."
)
FAILED_SUGGESTION = "Öneri"
FAILED_TIMEOUT = "Zaman aşımı"
FAILED_TIMEOUT_YES = "evet (sınır {limit}s)"
FAILED_RETRIES = "Denemeler tükendi"
FAILED_STATUS = "Durum"
FAILED_RETRY_COUNT = "Deneme sayısı"
FAILED_SESSION = "Oturum"
FAILED_FOOTER = (
    "Yukarıdaki ayrıntıları inceleyin ve nasıl devam edileceğini belirtin "
    "(örneğin yeniden denemek için kaydı *Yapılacaklar* sütununa alın)."
)
FAILED_EMPTY = "Bilinmeyen hata (ayrıntı yok)."
FAILED_DEFAULT_SUGGEST = (
    "Yeniden kuyruğa almak için kaydı Yapılacaklar’a alın veya "
    "YAVER_DATA_DIR/sessions/ altındaki oturum günlüklerine bakın."
)

ANSWER_HEADING = ""
ANSWER_EMPTY = (
    "_Ajan boş yanıt döndü. @bahsetmeyi yineleyin veya günlüklere bakın._"
)

USAGE_HEADING_TR = "**Yaver — komut nasıl çalıştırılır**"
USAGE_HEADING_EN = "**Yaver — how to run a command**"

USAGE_MR_PR = (
    "Yalnızca `/yaver` komutunu çalıştırırım. Aynı yorumda beni etiketleyin "
    "ve `/yaver` yazın.\n\n"
    "- `/yaver <istek>` — bu tartışmada bir iş başlatır\n"
)

USAGE_MR_PR_WITH_REVIEW = (
    "`/yaver`, `/review` ve `/ask` komutlarını çalıştırırım. Aynı yorumda "
    "beni etiketleyin ve komutu yazın.\n\n"
    "- `/yaver <istek>` — bu tartışmada bir iş başlatır\n"
    "- `/review` — bu birleştirme/çekme isteğini inceler (dosya değiştirmez)\n"
    "- `/ask <soru>` — inceleme konusuna takip sorusu\n"
)

USAGE_WORK_ITEM = (
    "İş öğesinde yalnızca plan komutlarını çalıştırırım. Aynı yorumda beni "
    "etiketleyin ve şunlardan birini yazın.\n\n"
    "- `/planRefactor <istek>` — bekleyen planı günceller\n"
    "- `/planExecute` — bekleyen planı uygular\n\n"
    "Yeni iş için bu öğeyi Yapılacaklar veya Devam Ediyor (veya New / "
    "Active / Doing) durumunda bana atayın, ya da Mode: build ile yeni "
    "bir öğe açın."
)

NO_PLAN_WAITING = (
    "Bu iş öğesinde bekleyen bir plan yok. "
    "`/planRefactor` ve `/planExecute` yalnızca Mode: plan işi "
    "plan_ready olduktan sonra çalışır. Yapılacaklar veya Devam Ediyor "
    "(veya New / Active / Doing) durumunda bir öğeyi bana atayın, "
    "ya da Mode: build ile yeni bir öğe açın."
)

PLAN_READY_WAIT_MR = (
    "Bu kayıt plan_ready bekliyor. Uygulamak için kayıt Devam Ediyor "
    "iken plan_ready etiketini plan_execute olarak yeniden adlandırın. "
    "Bu yorumdan bir uygulama işi başlatmadım."
)

ASSIGN_PAT_FAILED = (
    "PAT kullanıcısına atanamadı. Settings → Test aynı koleksiyon için "
    "çalışıyorsa PAT ve koleksiyon adresini kaydedip yeniden atayın. "
    "Atama olmadan iş başlatılmaz."
)
ASSIGN_PAT_FAILED_SUGGEST = (
    "Koleksiyon URL’sini ve PAT’i Settings’te kaydedin, Test’in geçtiğini "
    "doğrulayın, sonra öğeyi yeniden atayın."
)

SUGGEST_FIX_DESC_TODO = (
    "Kayıt açıklamasını düzeltin, sonra yeniden kuyruğa almak için "
    "kaydı *Yapılacaklar* sütununa alın."
)
SUGGEST_FIX_DESC_NO_IP = (
    "Kayıt açıklamasını düzeltin (ve bu iş akışında *Devam Ediyor* geçişi "
    "olduğundan emin olun). Hâlâ *Yapılacaklar* iken açıklamayı düzenleyin "
    "veya kaydı alıp yeniden *Yapılacaklar*’a alın."
)

TEMPLATE_HELP_TR = """\
{params}
Repository: https://gitlab.example.com/group/your-repo
Source branch: feature/PROJ-123
Target branch: develop
Mode: plan
Model: opencode/hy3-free
Backend: opencode
{params}

Mode isteğe bağlıdır (varsayılan ``build``):
* plan  — plan üretir ve Jira yorumuna yazar (GitLab’a gönderim yok)
* build — uygular / çalıştırır (dal gönderir + birleştirme isteği açar)
* test  — yalnızca birim testleri yazar (dal gönderir + birleştirme isteği açar)
Model ve Backend isteğe bağlıdır (varsayılan .env / Ayarlar).
"""


def params_missing_block(help_text: str) -> str:
    return (
        "*Yaver* başlayamadı: kayıtta ``{params}`` bloğu yok.\n\n"
        "Git ayarlarını *açıklamada* (veya özette) ``{params}`` işaretleri "
        "arasına yazın, sonra kaydı *Yapılacaklar*’a alın:\n\n"
        "{code}\n"
        f"{help_text.strip()}\n"
        "{code}"
    )


def params_incomplete_block(missing: str, help_text: str) -> str:
    return (
        "*Yaver* başlayamadı: kayıt açıklaması eksik veya geçersiz.\n\n"
        f"*Eksik / geçersiz:* {missing}.\n\n"
        "Açıklamaya *tüm* bu alanları içeren bir ``{params}`` bloğu ekleyin, "
        "sonra kaydı *Yapılacaklar*’a alın:\n\n"
        "{code}\n"
        f"{help_text.strip()}\n"
        "{code}"
    )


def params_bad_url_block(repo: str) -> str:
    return (
        "*Yaver* başlayamadı: depo adresi geçersiz görünüyor.\n\n"
        f"Okunan değer: `{repo}`\n\n"
        "``{params}`` içinde tam bir HTTPS (veya SSH) git adresi kullanın, "
        "örneğin:\n"
        "`Repository: https://gitlab.example.com/group/your-repo`"
    )


def fail_category(kind: str) -> tuple[str, str]:
    key = (kind or "error").strip().lower()
    if key in {"question", "clarifying_question", "clarification"}:
        return FAILED_QUESTION_H, FAILED_QUESTION
    if key in {"compact_loop", "autocompact_loop"}:
        return FAILED_COMPACT_H, FAILED_COMPACT
    if key == "incomplete":
        return FAILED_INCOMPLETE_H, FAILED_INCOMPLETE
    if key in {"unfinished", "todos", "open_todos"}:
        return FAILED_TODOS_H, FAILED_TODOS
    if key in {"thread_lock", "codex_lock", "active_writer"}:
        return FAILED_LOCK_H, FAILED_LOCK
    return FAILED_HEADING, FAILED_LEAD


def workflow_label(raw: Optional[str]) -> str:
    text = str(raw or "").strip()
    if not text or text.lower() == "unknown":
        return ""
    return text.replace("_", " ")
