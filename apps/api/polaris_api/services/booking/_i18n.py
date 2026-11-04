"""Static 3-language (en / ms / zh-cn) reply localization for the in-chat booking
picker.

Output localization ONLY — the intent/date/time/name/consent *parsing* is already
multilingual (Haiku + phrase tables). Here we render the picker's deterministic
replies in the customer's language, detected once on entry
(:func:`polaris_api.services.llm._detect_language`) and persisted on
``BookingPickerState.language``.

Design notes:
- **No glibc ``strftime``.** Weekday/month/AM-PM are localized by indexing explicit
  per-language tuples with ``date.weekday()`` / ``date.month`` (``strftime('%a'/'%b')``
  is locale-dependent and unreliable on Windows, where the suite runs).
- **EN values stay close to the original copy** so the existing English reply
  assertions keep passing. Em dashes were removed from the prose (split into
  separate sentences, or commas / colons); only the words around the
  deterministic bits (times, emails, links, the numeric ranges) translate.
- ``t()`` falls back to ``en`` for any missing ``(key, lang)`` so a gap degrades to
  English rather than raising at runtime; a unit test asserts full coverage.
"""

from __future__ import annotations

from typing import Literal

Lang = Literal["en", "ms", "zh-cn"]
LANGS: tuple[Lang, ...] = ("en", "ms", "zh-cn")


def coerce_lang(value: object) -> Lang:
    """Narrow an arbitrary value to a supported ``Lang`` (default ``"en"``)."""
    if value in LANGS:
        return value
    return "en"


# date.weekday(): Mon=0 … Sun=6
WEEKDAYS: dict[Lang, tuple[str, ...]] = {
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "ms": ("Isn", "Sel", "Rab", "Kha", "Jum", "Sab", "Ahd"),
    "zh-cn": ("周一", "周二", "周三", "周四", "周五", "周六", "周日"),
}
# date.month: 1..12 (index 0 unused).
MONTHS: dict[Lang, tuple[str, ...]] = {
    "en": ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "ms": ("", "Jan", "Feb", "Mac", "Apr", "Mei", "Jun",
           "Jul", "Ogos", "Sep", "Okt", "Nov", "Dis"),
    "zh-cn": ("", "1月", "2月", "3月", "4月", "5月", "6月",
              "7月", "8月", "9月", "10月", "11月", "12月"),
}
AMPM: dict[Lang, tuple[str, str]] = {  # (am, pm)
    "en": ("AM", "PM"),
    "ms": ("pagi", "petang"),
    "zh-cn": ("上午", "下午"),
}
AND: dict[Lang, str] = {"en": "and", "ms": "dan", "zh-cn": "和"}


# Message registry: key -> {lang -> template}. EN is the regression anchor (verbatim
# from conversational.py). Placeholders are named and identical across languages.
T: dict[str, dict[Lang, str]] = {
    # --- availability / ranges ---
    "range_offer_with_label": {
        "en": "On {label} we're open {windows}. What time works for you?",
        "ms": "Pada {label} kami buka {windows}. Pukul berapa sesuai untuk anda?",
        "zh-cn": "{label}我们的营业时间是 {windows}。您想约几点？",
    },
    "range_offer_no_label": {
        "en": "we're open {windows}. What time works for you?",
        "ms": "Kami buka {windows}. Pukul berapa sesuai untuk anda?",
        "zh-cn": "我们的营业时间是 {windows}。您想约几点？",
    },
    "range_none": {
        "en": "I don't see any open times then. Want to try another date?",
        "ms": "Maaf, slot pada waktu tersebut telah penuh. Boleh kami cadangkan tarikh lain?",
        "zh-cn": "不好意思，那天已经满了哦，要不要换别的日期？",
    },
    # --- date prompts ---
    "offer_date_prompt": {
        "en": 'Sure, what date and time works for you? '
        '(e.g. "30 Jun at 2pm" or "next Monday afternoon")',
        "ms": "Baik, encik/puan cadang nak slot hari apa dan pukul berapa? "
        '(cth. "30 Jun pukul 2 petang" atau "Isnin depan petang")',
        "zh-cn": "没问题，你想约几时？（例如「7月1号下午2点」或者「下礼拜一下午」）",
    },
    "offer_link_tail": {
        "en": "\n\nOr book here anytime:\n{url}",
        "ms": "\n\nAtau tempah di sini bila-bila masa:\n{url}",
        "zh-cn": "\n\n也可以随时点此预约：\n{url}",
    },
    "ask_date_again": {
        "en": 'Sure, what date and time works for you? '
        '(e.g. "next Monday at 2pm" or "30 Jun")',
        "ms": "Baik, encik/puan cadang nak slot hari apa dan pukul berapa? "
        '(cth. "Isnin depan pukul 2 petang" atau "30 Jun")',
        "zh-cn": "没问题，你想约几时？（例如「下礼拜一下午2点」或者「7月1号」）",
    },
    "didnt_catch_date": {
        "en": 'I didn\'t catch a date. Try "30 Jun at 2pm" or "next Monday" '
        "(or use the booking link).",
        "ms": "Maaf, sistem kurang jelas dengan tarikh tersebut. Sila cuba "
        'format seperti "30 Jun pukul 2 petang" atau "Isnin depan" '
        "(atau klik sahaja pautan tempahan ini).",
        "zh-cn": "不好意思，我抓不到你的日期，可以试下写「7月1号下午2点」或者「下礼拜一」（或者直接用 Booking Link 预约）。",
    },
    # --- slot echo / details capture ---
    "slot_echo": {
        "en": "Got it: {pretty}.",
        "ms": "Baik, butiran tempahan: {pretty}.",
        "zh-cn": "收到：{pretty}。",
    },
    "slot_echo_nearest": {
        "en": "{requested} isn't open, so I've noted the nearest: {pretty}.",
        "ms": "{requested} tidak tersedia, jadi saya ambil yang terdekat: {pretty}.",
        "zh-cn": "{requested} 没有空档，所以我帮您记下最接近的：{pretty}。",
    },
    "almost_there": {
        "en": "{echo} Almost there, {ask}",
        "ms": "{echo} Proses tempahan hampir selesai, {ask}",
        "zh-cn": "{echo} 快要搞定了，{ask}",
    },
    "details_ask": {
        "en": "what's your {labels}?",
        "ms": "boleh beri {labels} anda?",
        "zh-cn": "请告诉我您的{labels}？",
    },
    "details_still_need": {
        "en": "I still need your {labels}.",
        "ms": "Saya masih perlukan {labels} anda.",
        "zh-cn": "还需要您的{labels}。",
    },
    # detail field labels (joined by AND[lang] via _join_and)
    "label_name": {"en": "name", "ms": "nama", "zh-cn": "姓名"},
    "label_email": {"en": "email", "ms": "emel", "zh-cn": "电邮"},
    "label_phone": {
        "en": "phone number",
        "ms": "nombor telefon",
        "zh-cn": "电话号码",
    },
    # --- summary / confirm ---
    "summary": {
        "en": "Please confirm your booking:\n  • {pretty}\n  • {name} · {email} · {phone}"
        "\n\nShall I confirm this booking?",
        "ms": "Sila sahkan tempahan anda:\n  • {pretty}\n  • {name} · {email} · {phone}"
        "\n\nSahkan tempahan ini?",
        "zh-cn": "请确认您的预约：\n  • {pretty}\n  • {name} · {email} · {phone}"
        "\n\n确认这个预约吗？",
    },
    "lead_thanks": {"en": "Thank you!", "ms": "Terima kasih!", "zh-cn": "谢谢！"},
    "lead_updated": {"en": "Updated.", "ms": "Dikemas kini.", "zh-cn": "已更新。"},
    "confirm_no": {
        "en": "No problem. Want a different time, or shall I leave it? "
        "(Reply *cancel* to stop, or tell me a new time.)",
        "ms": "Tiada masalah. Adakah anda ingin menukar masa atau kekalkan sahaja? "
        "(Balas *cancel* untuk batal, atau maklumkan masa baharu kepada kami.)",
        "zh-cn": "没问题，你是想换时间，还是照旧先？（回复 *cancel* 取消，或者写下新的时间给我就行。）",
    },
    "confirm_unclear": {
        "en": "Shall I confirm this booking? Or tell me what to change: "
        "the time, your name, email or phone.",
        "ms": "Sila sahkan tempahan ini. Atau maklumkan jika ada butiran yang "
        "ingin diubah: masa, nama, e-mel atau nombor telefon.",
        "zh-cn": "确认这个预约吗？还是要改什么：时间、名字、Email 还是电话号码？",
    },
    # --- booked ---
    "booked_named": {
        "en": "Booked! ✅ {name}, you're confirmed for {pretty}. "
        "Reply *RESCHEDULE* or *CANCEL* any time if your plans change.",
        "ms": "Tempahan disahkan! ✅ {name}, anda ditempah untuk {pretty}. "
        "Balas *RESCHEDULE* atau *CANCEL* bila-bila masa jika rancangan berubah.",
        "zh-cn": "预约成功！✅ {name}，您已预约 {pretty}。"
        "如需更改请随时回复 *RESCHEDULE* 或 *CANCEL*。",
    },
    "booked_unnamed": {
        "en": "Booked! ✅ You're confirmed for {pretty}. "
        "Reply *RESCHEDULE* or *CANCEL* any time if your plans change.",
        "ms": "Tempahan disahkan! ✅ Anda ditempah untuk {pretty}. "
        "Balas *RESCHEDULE* atau *CANCEL* bila-bila masa jika rancangan berubah.",
        "zh-cn": "预约成功！✅ 您已预约 {pretty}。"
        "如需更改请随时回复 *RESCHEDULE* 或 *CANCEL*。",
    },
    # --- bail ---
    "bail_generic": {
        "en": "No problem. Message me whenever you'd like to book.",
        "ms": "Tiada masalah. Hubungi kami semula pada bila-bila masa jika anda ingin membuat tempahan.",
        "zh-cn": "没问题，下次想预约的话随时联络我。",
    },
    "bail_not_booked": {
        "en": "No problem. I haven't booked anything. Message me whenever you'd like to.",
        "ms": "Tiada masalah, tiada sebarang tempahan aktif buat masa ini. Hubungi kami semula jika anda memerlukan bantuan.",
        "zh-cn": "没问题，现在还没有看到任何预约。有需要的话随时联络我。",
    },
    # --- re-prompts / errors ---
    "didnt_catch_time": {
        "en": "I didn't catch a time. ",
        "ms": "Saya tak pasti masanya. ",
        "zh-cn": "我没听清楚时间。",
    },
    "slot_unavailable": {
        "en": "Sorry, that slot is no longer available. ",
        "ms": "Maaf, slot itu tidak lagi tersedia. ",
        "zh-cn": "抱歉，该时段已经约满。",
    },
    "start_over": {
        "en": "Sorry, let's start over. What would you like to book?",
        "ms": "Maaf atas kesulitan, mari kita mulakan semula. Anda ingin membuat tempahan untuk apa ya?",
        "zh-cn": "不好意思，我们可以重新来过，你想预约几时呢？",
    },
    "went_wrong": {
        "en": "Sorry, something went wrong with your booking. Please try again.",
        "ms": "Maaf, ada masalah dengan tempahan anda. Sila cuba lagi.",
        "zh-cn": "抱歉，预约出了点问题。请再试一次。",
    },
    "no_back_and_forth": {
        "en": "No worries. Let's not go back and forth.{tail}",
        "ms": "Baik, tidak mengapa. Kami tidak mahu mengambil masa anda lagi.{tail}",
        "zh-cn": "没关系，那我们就不打扰先。{tail}",
    },
    # --- no-availability ---
    "no_avail_fully_booked": {
        "en": "Sorry, {label} is fully booked.",
        "ms": "Maaf, {label} sudah penuh.",
        "zh-cn": "抱歉，{label}已约满。",
    },
    "no_avail_closed": {
        "en": "Sorry, we're closed on {label}.",
        "ms": "Maaf, kami tutup pada {label}.",
        "zh-cn": "抱歉，{label}我们休息。",
    },
    "nearest_opening_lead": {
        "en": "{prefix} The nearest opening is {pretty}.",
        "ms": "{prefix} Slot terdekat ialah {pretty}.",
        "zh-cn": "{prefix} 最近的空档是 {pretty}。",
    },
    "no_openings": {
        "en": "{prefix} I don't see any openings in the next two weeks.{tail}",
        "ms": "{prefix} Saya tak nampak slot kosong dalam dua minggu akan datang.{tail}",
        "zh-cn": "{prefix} 未来两周内没有空档。{tail}",
    },
    # --- link / help tails ---
    "tail_other_dates": {
        "en": "\n\nPlease use this link to see other dates:\n{url}",
        "ms": "\n\nSila guna pautan ini untuk lihat tarikh lain:\n{url}",
        "zh-cn": "\n\n请用此链接查看其他日期：\n{url}",
    },
    "tail_finish_here": {
        "en": "\n\nYou can finish your booking here:\n{url}",
        "ms": "\n\nAnda boleh selesaikan tempahan di sini:\n{url}",
        "zh-cn": "\n\n您可以在此完成预约：\n{url}",
    },
    "tail_help": {
        "en": "\n\nReply *HELP* to reach our team.",
        "ms": "\n\nBalas *HELP* untuk hubungi pasukan kami.",
        "zh-cn": "\n\n回复 *HELP* 联系我们的团队。",
    },
}


def t(key: str, lang: object, /, **kw: object) -> str:
    """Render message ``key`` for ``lang`` (fallback ``en``), then ``.format(**kw)``."""
    row = T[key]
    lang_ = coerce_lang(lang)
    template = row.get(lang_, row["en"])
    return template.format(**kw) if kw else template


def join_and(items: list[str], lang: object) -> str:
    """``["a","b","c"] → "a, b and c"`` with the localized conjunction.

    CJK uses the 顿号 ``、`` separator and no spaces around 和 (``"姓名、电邮和电话号码"``);
    en/ms use Oxford-free comma joins (``"name, email and phone number"``).
    """
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    lang_ = coerce_lang(lang)
    conj = AND[lang_]
    if lang_ == "zh-cn":
        if len(items) == 2:
            return f"{items[0]}{conj}{items[1]}"
        return "、".join(items[:-1]) + f"{conj}{items[-1]}"
    if len(items) == 2:
        return f"{items[0]} {conj} {items[1]}"
    # Oxford-free (matches the picker's original _join_and): "a, b and c".
    return ", ".join(items[:-1]) + f" {conj} {items[-1]}"


__all__ = [
    "AMPM",
    "AND",
    "LANGS",
    "MONTHS",
    "T",
    "WEEKDAYS",
    "Lang",
    "coerce_lang",
    "join_and",
    "t",
]
