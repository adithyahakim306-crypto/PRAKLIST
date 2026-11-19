import os
import re
import csv
import asyncio
import logging
from io import BytesIO
import shutil

from dotenv import load_dotenv

load_dotenv()  # baca file .env (harus sebelum os.environ[...] di bawah)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    PicklePersistence,
)

from keyboards import (
    CODE_TO_CATEGORY,
    CATEGORY_CODES,
    KPOP_CATEGORIES,
    CODE_TO_GALLERY,
    load_groups,
    category_keyboard,
    edit_cancel_keyboard,
    group_keyboard,
    member_keyboard,
    gallery_keyboard_kpop,
    gallery_keyboard_text,
    gallery_keyboard_lihat,
    category_keyboard_lihat,
    group_keyboard_lihat,
    back_only_keyboard,
)
import sheets
import ocr

logging.basicConfig(level=logging.INFO)
# httpx mencetak URL request yang berisi token bot, jadi dibisukan.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROUPS_PATH = os.environ.get("GROUPS_PATH", "groups.csv")
if not os.path.exists(GROUPS_PATH):
    os.makedirs(os.path.dirname(GROUPS_PATH) or ".", exist_ok=True)
    shutil.copy("groups.csv", GROUPS_PATH)

GROUPS = load_groups(GROUPS_PATH)


# ---------- Command dasar ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Halo! Kirim screenshot daftar username buat dikategorikan.\n"
        "Ketik /lihat buat cek rekap yang sudah ada.\n"
        "Kirim @username buat cek lokasi kategorinya."
    )


async def lihat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Pilih gallery yang mau dilihat:", reply_markup=gallery_keyboard_lihat()
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tambah 1 username manual tanpa screenshot: /add @username"""
    if not context.args:
        await update.message.reply_text(
            "Pakai format: /add @username\nContoh: /add @Minwooky"
        )
        return

    username = context.args[0].lstrip("@").strip()
    if not username:
        await update.message.reply_text(
            "Pakai format: /add @username\nContoh: /add @Minwooky"
        )
        return

    # Tambahkan ke antrian yang sedang berjalan (kalau ada), atau mulai antrian baru.
    pending = context.user_data.setdefault("pending", [])
    context.user_data.setdefault("nama_store", {})
    index = len(pending)
    pending.append(username)

    await update.message.reply_text(
        f"@{username}\nPilih kategori:", reply_markup=category_keyboard(index)
    )


async def hapus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cari semua lokasi username, lalu kasih tombol hapus per-lokasi: /hapus @username"""
    if not context.args:
        await update.message.reply_text(
            "Pakai format: /hapus @username\nContoh: /hapus @Minwooky"
        )
        return

    username = context.args[0].lstrip("@").strip()
    if not username:
        await update.message.reply_text(
            "Pakai format: /hapus @username\nContoh: /hapus @Minwooky"
        )
        return

    try:
        entries = await asyncio.to_thread(sheets.find_entries, username)
    except Exception as e:
        logger.error("Gagal baca dari Sheets: %s", e)
        await update.message.reply_text("Gagal cek rekap, coba lagi sebentar lagi.")
        return

    if not entries:
        await update.message.reply_text(f"@{username} tidak ditemukan di rekap.")
        return

    lines = [f"@{username} ditemukan di {len(entries)} lokasi:"]
    buttons = []
    for i, (row_index, row) in enumerate(entries, start=1):
        lokasi = row.get("Kategori", "")
        if row.get("Grup"):
            lokasi += f" / {row['Grup']}"
        if row.get("Nama"):
            lokasi += f" / {row['Nama']}"
        if row.get("Gallery"):
            lokasi += f" (@{row['Gallery']})"
        lines.append(f"{i}. {lokasi}")
        buttons.append(
            [InlineKeyboardButton(f"🗑 Hapus #{i}", callback_data=f"delhap|{row_index}|{username}")]
        )
    buttons.append([InlineKeyboardButton("❌ Batal", callback_data="delhap_batal")])

    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


# ---------- /hps: hapus seluruh isi satu gallery ----------

async def hps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [InlineKeyboardButton(f"@{gallery}", callback_data=f"hpsgal|{code}")]
        for code, gallery in CODE_TO_GALLERY.items()
    ]
    buttons.append([InlineKeyboardButton("❌ Batal", callback_data="hpsno")])
    await update.message.reply_text(
        "Pilih gallery yang SELURUH list-nya mau dihapus:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_hps_gallery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gallery dipilih -> minta konfirmasi dulu."""
    query = update.callback_query
    await query.answer()
    _, gal_code = query.data.split("|")
    gallery = CODE_TO_GALLERY[gal_code]

    try:
        jumlah = await asyncio.to_thread(sheets.count_entries_by_gallery, gallery)
    except Exception as e:
        logger.error("Gagal baca dari Sheets: %s", e)
        await query.edit_message_text("Gagal cek rekap, coba lagi sebentar lagi.")
        return

    if jumlah == 0:
        await query.edit_message_text(f"Gallery @{gallery} sudah kosong, tidak ada yang dihapus.")
        return

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🗑 Ya, hapus {jumlah} entri", callback_data=f"hpsyes|{gal_code}")],
            [InlineKeyboardButton("❌ Batal", callback_data="hpsno")],
        ]
    )
    await query.edit_message_text(
        f"⚠️ Yakin mau hapus SEMUA ({jumlah} entri) dari gallery @{gallery}?\n"
        "Tindakan ini tidak bisa dibatalkan.",
        reply_markup=keyboard,
    )


async def handle_hps_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, gal_code = query.data.split("|")
    gallery = CODE_TO_GALLERY[gal_code]

    try:
        jumlah = await asyncio.to_thread(sheets.delete_entries_by_gallery, gallery)
    except Exception as e:
        logger.error("Gagal hapus gallery dari Sheets: %s", e)
        await query.edit_message_text("Gagal hapus dari rekap, coba lagi sebentar lagi.")
        return

    await query.edit_message_text(f"✅ {jumlah} entri dari gallery @{gallery} sudah dihapus.")


async def handle_hps_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Dibatalkan.")


# ---------- /group: tambah grup & member baru ke groups.csv ----------

GROUP_CATEGORIES = {"BG": "Kpop Boygroup", "GG": "Kpop Girlgroup"}
# callback_data Telegram maksimal 64 byte, dan tombol gallery memuat grup + member
# sekaligus, jadi panjang gabungan keduanya dibatasi.
MAX_GRUP_MEMBER_BYTES = 48
MAX_GRUP_BYTES = 30


def _group_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="ngrpbatal")]])


def save_group_to_csv(kategori, grup, members):
    """Tulis member baru ke groups.csv lalu update GROUPS di memori.
    Return list member yang benar-benar baru (yang sudah ada dilewati)."""
    existing = GROUPS.get(kategori, {}).get(grup, [])
    existing_lower = {m.lower() for m in existing}
    new_members = []
    for m in members:
        if m.lower() not in existing_lower:
            existing_lower.add(m.lower())
            new_members.append(m)
    if not new_members:
        return []

    with open(GROUPS_PATH, newline="", encoding="utf-8") as f:
        fieldnames = csv.DictReader(f).fieldnames or ["kategori", "grup", "member"]

    # Pastikan baris terakhir file sudah diakhiri newline sebelum append
    with open(GROUPS_PATH, "rb+") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() > 0:
            f.seek(-1, os.SEEK_END)
            if f.read(1) not in (b"\n", b"\r"):
                f.write(b"\n")

    with open(GROUPS_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        for m in new_members:
            writer.writerow({"kategori": kategori, "grup": grup, "member": m})

    GROUPS.setdefault(kategori, {}).setdefault(grup, []).extend(new_members)
    return new_members


async def group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_group"] = None
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Boygroup", callback_data="ngrpcat|BG"),
                InlineKeyboardButton("Girlgroup", callback_data="ngrpcat|GG"),
            ],
            [InlineKeyboardButton("❌ Batal", callback_data="ngrpbatal")],
        ]
    )
    await update.message.reply_text("Mau tambah grup ke kategori mana?", reply_markup=keyboard)


async def handle_ngrp_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, cat_code = query.data.split("|")
    kategori = GROUP_CATEGORIES[cat_code]
    context.user_data["awaiting_group"] = {"step": "name", "kategori": kategori}
    await query.edit_message_text(
        f"Kategori: {kategori}\nKetik nama grupnya:",
        reply_markup=_group_cancel_keyboard(),
    )


async def handle_ngrp_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting_group"] = None
    await query.edit_message_text("Dibatalkan.")


async def handle_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE, state, text):
    """Dipanggil dari handle_text selama user ada di alur /group."""
    kategori = state["kategori"]

    # --- Langkah 1: nama grup ---
    if state["step"] == "name":
        grup = " ".join(text.split())
        if not grup or "|" in grup:
            await update.message.reply_text(
                "Nama grup tidak valid (jangan kosong atau pakai tanda |). Coba ketik lagi.",
                reply_markup=_group_cancel_keyboard(),
            )
            return
        if len(grup.encode("utf-8")) > MAX_GRUP_BYTES:
            await update.message.reply_text(
                f"Nama grup terlalu panjang (maks {MAX_GRUP_BYTES} karakter). Coba ketik lagi.",
                reply_markup=_group_cancel_keyboard(),
            )
            return

        # Kalau grup sudah ada (beda huruf besar/kecil saja), pakai penulisan yang sudah ada
        for existing_name in GROUPS.get(kategori, {}):
            if existing_name.lower() == grup.lower():
                grup = existing_name
                break

        state["grup"] = grup
        state["step"] = "members"
        info = ""
        if grup in GROUPS.get(kategori, {}):
            info = f"\n(Grup ini sudah ada dengan {len(GROUPS[kategori][grup])} member, member baru akan ditambahkan.)"
        await update.message.reply_text(
            f"Grup: {grup} ({kategori}){info}\n"
            "Sekarang ketik nama anggotanya.\n"
            "Bisa banyak sekaligus, pisahkan dengan koma atau baris baru.",
            reply_markup=_group_cancel_keyboard(),
        )
        return

    # --- Langkah 2: nama anggota ---
    grup = state["grup"]
    members = [m.strip() for m in re.split(r"[,\n]", text) if m.strip()]
    if not members:
        await update.message.reply_text(
            "Belum ada nama anggota yang terbaca. Coba ketik lagi.",
            reply_markup=_group_cancel_keyboard(),
        )
        return

    invalid = [
        m for m in members
        if "|" in m or len(grup.encode("utf-8")) + len(m.encode("utf-8")) > MAX_GRUP_MEMBER_BYTES
    ]
    if invalid:
        await update.message.reply_text(
            "Nama anggota ini terlalu panjang atau mengandung tanda |:\n"
            + "\n".join(f"- {m}" for m in invalid)
            + "\n\nPersingkat namanya lalu kirim ulang daftar anggotanya.",
            reply_markup=_group_cancel_keyboard(),
        )
        return

    try:
        baru = await asyncio.to_thread(save_group_to_csv, kategori, grup, members)
    except Exception as e:
        logger.error("Gagal simpan ke groups.csv: %s", e)
        await update.message.reply_text("Gagal menyimpan ke groups.csv, coba lagi sebentar lagi.")
        return

    context.user_data["awaiting_group"] = None
    if not baru:
        await update.message.reply_text(
            f"Semua anggota itu sudah ada di {grup}, tidak ada yang ditambahkan."
        )
        return

    pesan = f"✅ {grup} ({kategori}) tersimpan dengan {len(baru)} member:\n" + "\n".join(
        f"- {m}" for m in baru
    )
    dilewati = len(members) - len(baru)
    if dilewati:
        pesan += f"\n\n{dilewati} member dilewati karena sudah ada."
    pesan += "\n\nLangsung muncul di pilihan grup, tidak perlu restart bot."
    await update.message.reply_text(pesan)


# ---------- Alur input dari screenshot ----------

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Lagi baca gambar...")

    msg = update.message
    # Foto biasa dikompres Telegram (hasil OCR lebih jelek). Gambar yang
    # dikirim sebagai "File" tidak dikompres, jadi lebih akurat.
    file_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
    file = await context.bot.get_file(file_id)
    buf = BytesIO()
    await file.download_to_memory(out=buf)

    try:
        usernames = await asyncio.to_thread(ocr.extract_usernames_from_image, buf.getvalue())
    except Exception as e:
        logger.error("OCR error: %s", e)
        await update.message.reply_text("Gagal proses OCR, coba lagi sebentar lagi.")
        return

    if not usernames:
        await update.message.reply_text(
            "Gagal baca username dari gambar. Coba kirim ulang screenshot yang lebih jelas."
        )
        return

    context.user_data["pending"] = usernames
    context.user_data["awaiting_text"] = None
    context.user_data["nama_store"] = {}
    context.user_data["skipped"] = []
    context.user_data["awaiting_edit"] = None
    await send_next_username(update.effective_chat.id, context, 0)


async def send_next_username(chat_id, context, index):
    pending = context.user_data.get("pending", [])
    if index >= len(pending):
        pesan = "Selesai! Semua username sudah dikategorikan."
        skipped = context.user_data.get("skipped", [])
        if skipped:
            pesan += (
                "\n\nDilewati (ditandai salah):\n"
                + "\n".join(f"@{u}" for u in skipped)
                + "\n\nKalau mau diinput manual: /add @username"
            )
        await context.bot.send_message(chat_id, pesan)
        context.user_data["pending"] = []
        context.user_data["nama_store"] = {}
        context.user_data["skipped"] = []
        return
    username = pending[index]
    await context.bot.send_message(
        chat_id,
        f"@{username}\nPilih kategori:",
        reply_markup=category_keyboard(index),
    )


async def handle_salah_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol 'Salah': buang username ini saja, langsung lanjut ke berikutnya."""
    query = update.callback_query
    await query.answer()
    _, index_str = query.data.split("|")
    index = int(index_str)

    pending = context.user_data.get("pending", [])
    if index >= len(pending):
        await query.edit_message_text("Antrian ini sudah tidak berlaku.")
        return
    username = pending[index]

    awaiting = context.user_data.get("awaiting_text")
    if awaiting and awaiting.get("index") == index:
        context.user_data["awaiting_text"] = None
    editing = context.user_data.get("awaiting_edit")
    if editing and editing.get("index") == index:
        context.user_data["awaiting_edit"] = None
    context.user_data.get("nama_store", {}).pop(index, None)
    context.user_data.setdefault("skipped", []).append(username)

    await query.edit_message_text(f"❌ @{username} dilewati (ditandai salah).")
    await send_next_username(update.effective_chat.id, context, index + 1)


async def handle_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol 'Edit': minta user mengetik username yang benar."""
    query = update.callback_query
    await query.answer()
    _, index_str = query.data.split("|")
    index = int(index_str)

    pending = context.user_data.get("pending", [])
    if index >= len(pending):
        await query.edit_message_text("Antrian ini sudah tidak berlaku.")
        return
    username = pending[index]

    context.user_data["awaiting_text"] = None
    context.user_data["awaiting_edit"] = {
        "index": index,
        "message_id": query.message.message_id,
    }
    await query.edit_message_text(
        f"@{username}\nKetik username yang benar (pakai @ atau tanpa @):",
        reply_markup=edit_cancel_keyboard(index),
    )


async def handle_editbatal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, index_str = query.data.split("|")
    index = int(index_str)
    context.user_data["awaiting_edit"] = None

    pending = context.user_data.get("pending", [])
    if index >= len(pending):
        await query.edit_message_text("Antrian ini sudah tidak berlaku.")
        return
    await query.edit_message_text(
        f"@{pending[index]}\nPilih kategori:", reply_markup=category_keyboard(index)
    )


async def handle_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, index_str, cat_code = query.data.split("|")
    index = int(index_str)
    kategori = CODE_TO_CATEGORY[cat_code]

    pending = context.user_data.get("pending", [])
    if index >= len(pending):
        return
    username = pending[index]

    if kategori in KPOP_CATEGORIES:
        await query.edit_message_text(
            f"@{username}\nKategori: {kategori}\nPilih grup:",
            reply_markup=group_keyboard(index, kategori, GROUPS),
        )
    else:
        context.user_data["awaiting_text"] = {"index": index, "kategori": kategori}
        await query.edit_message_text(
            f"@{username}\nKategori: {kategori}\nBalas pesan ini dengan nama / based on-nya:"
        )


async def handle_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, index_str, cat_code, grup = query.data.split("|")
    index = int(index_str)
    kategori = CODE_TO_CATEGORY[cat_code]

    pending = context.user_data.get("pending", [])
    username = pending[index]

    await query.edit_message_text(
        f"@{username}\nKategori: {kategori}\nGrup: {grup}\nPilih member:",
        reply_markup=member_keyboard(index, kategori, grup, GROUPS),
    )


async def handle_member_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Member dipilih -> lanjut pilih gallery dulu, belum simpan."""
    query = update.callback_query
    await query.answer()
    _, index_str, cat_code, grup, member = query.data.split("|")
    index = int(index_str)
    kategori = CODE_TO_CATEGORY[cat_code]

    pending = context.user_data.get("pending", [])
    username = pending[index]

    await query.edit_message_text(
        f"@{username}\nKategori: {kategori}\nGrup: {grup}\nNama: {member}\nSimpan ke gallery mana?",
        reply_markup=gallery_keyboard_kpop(index, kategori, grup, member),
    )


async def handle_gallery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gallery dipilih (alur kpop) -> simpan ke Sheets."""
    query = update.callback_query
    await query.answer()
    _, index_str, cat_code, grup, member, gal_code = query.data.split("|")
    index = int(index_str)
    kategori = CODE_TO_CATEGORY[cat_code]
    gallery = CODE_TO_GALLERY[gal_code]

    pending = context.user_data.get("pending", [])
    username = pending[index]

    try:
        await asyncio.to_thread(sheets.add_entry, username, kategori, grup, member, gallery)
    except Exception as e:
        logger.error("Gagal simpan ke Sheets: %s", e)
        await query.edit_message_text("Gagal simpan ke rekap, coba lagi sebentar lagi.")
        return
    await query.edit_message_text(
        f"Tersimpan: @{username} -> {kategori} / {grup} / {member} (@{gallery})"
    )
    await send_next_username(update.effective_chat.id, context, index + 1)


async def handle_galtxt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gallery dipilih (alur non-kpop, nama dari balasan teks) -> simpan ke Sheets."""
    query = update.callback_query
    await query.answer()
    _, index_str, cat_code, gal_code = query.data.split("|")
    index = int(index_str)
    kategori = CODE_TO_CATEGORY[cat_code]
    gallery = CODE_TO_GALLERY[gal_code]

    pending = context.user_data.get("pending", [])
    username = pending[index]
    nama = context.user_data.get("nama_store", {}).get(index, "")

    try:
        await asyncio.to_thread(sheets.add_entry, username, kategori, "", nama, gallery)
    except Exception as e:
        logger.error("Gagal simpan ke Sheets: %s", e)
        await query.edit_message_text("Gagal simpan ke rekap, coba lagi sebentar lagi.")
        return
    context.user_data.get("nama_store", {}).pop(index, None)
    await query.edit_message_text(f"Tersimpan: @{username} - {nama} ({kategori}) (@{gallery})")
    await send_next_username(update.effective_chat.id, context, index + 1)


# ---------- Tombol Kembali ----------

async def handle_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|")
    target = parts[1]

    pending = context.user_data.get("pending", [])

    if target == "cat":
        index = int(parts[2])
        username = pending[index]
        await query.edit_message_text(
            f"@{username}\nPilih kategori:", reply_markup=category_keyboard(index)
        )

    elif target == "grp":
        index = int(parts[2])
        cat_code = parts[3]
        kategori = CODE_TO_CATEGORY[cat_code]
        username = pending[index]
        await query.edit_message_text(
            f"@{username}\nKategori: {kategori}\nPilih grup:",
            reply_markup=group_keyboard(index, kategori, GROUPS),
        )

    elif target == "mem":
        index = int(parts[2])
        cat_code = parts[3]
        grup = parts[4]
        kategori = CODE_TO_CATEGORY[cat_code]
        username = pending[index]
        await query.edit_message_text(
            f"@{username}\nKategori: {kategori}\nGrup: {grup}\nPilih member:",
            reply_markup=member_keyboard(index, kategori, grup, GROUPS),
        )

    elif target == "gallihat":
        await query.edit_message_text(
            "Pilih gallery yang mau dilihat:", reply_markup=gallery_keyboard_lihat()
        )

    elif target == "catlihat":
        gal_code = parts[2]
        gallery = CODE_TO_GALLERY[gal_code]
        await query.edit_message_text(
            f"Gallery: @{gallery}\nPilih kategori:",
            reply_markup=category_keyboard_lihat(gallery),
        )

    elif target == "grplihat":
        gal_code = parts[2]
        cat_code = parts[3]
        gallery = CODE_TO_GALLERY[gal_code]
        kategori = CODE_TO_CATEGORY[cat_code]
        await query.edit_message_text(
            f"Gallery: @{gallery}\nKategori: {kategori}\nPilih grup:",
            reply_markup=group_keyboard_lihat(gallery, kategori, GROUPS),
        )


# ---------- Pesan teks: jawaban "based on" ATAU cek @username ----------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    group_state = context.user_data.get("awaiting_group")
    if group_state:
        await handle_group_text(update, context, group_state, text)
        return

    editing = context.user_data.get("awaiting_edit")
    if editing:
        index = editing["index"]
        pending = context.user_data.get("pending", [])
        if index >= len(pending):
            context.user_data["awaiting_edit"] = None
        else:
            new_username = text.lstrip("@").strip()
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", new_username):
                await update.message.reply_text(
                    "Username tidak valid (5-32 karakter, huruf/angka/_, diawali huruf). "
                    "Coba ketik lagi, atau tekan Batal edit."
                )
                return
            pending[index] = new_username
            context.user_data["awaiting_edit"] = None
            # Edit pesan yang sama supaya tidak ada pesan lama dengan tombol basi
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=editing["message_id"],
                text=f"@{new_username}\nPilih kategori:",
                reply_markup=category_keyboard(index),
            )
            return

    awaiting = context.user_data.get("awaiting_text")

    if awaiting:
        index = awaiting["index"]
        kategori = awaiting["kategori"]
        pending = context.user_data.get("pending", [])
        username = pending[index]

        context.user_data["awaiting_text"] = None
        context.user_data.setdefault("nama_store", {})[index] = text
        await update.message.reply_text(
            f"@{username}\nKategori: {kategori}\nNama: {text}\nSimpan ke gallery mana?",
            reply_markup=gallery_keyboard_text(index, kategori),
        )
        return

    if text.startswith("@"):
        username = text[1:]
        try:
            row_index, row = await asyncio.to_thread(sheets.find_entry, username)
        except Exception as e:
            logger.error("Gagal baca dari Sheets: %s", e)
            await update.message.reply_text("Gagal cek rekap, coba lagi sebentar lagi.")
            return
        if row is None:
            await update.message.reply_text(f"@{username} tidak ditemukan di rekap.")
            return

        lokasi = row["Kategori"]
        if row.get("Grup"):
            lokasi += f" / {row['Grup']}"
        if row.get("Nama"):
            lokasi += f" / {row['Nama']}"
        if row.get("Gallery"):
            lokasi += f" (@{row['Gallery']})"

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Hapus dari rekap", callback_data=f"del|{row_index}|{username}")]]
        )
        await update.message.reply_text(f"@{username} ada di: {lokasi}", reply_markup=keyboard)
        return

    await update.message.reply_text(
        "Kirim screenshot buat dikategorikan, atau ketik @username buat cek lokasinya."
    )


async def handle_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, row_index_str, username = query.data.split("|")
    try:
        await asyncio.to_thread(sheets.delete_entry, int(row_index_str))
    except Exception as e:
        logger.error("Gagal hapus dari Sheets: %s", e)
        await query.edit_message_text("Gagal hapus dari rekap, coba lagi sebentar lagi.")
        return
    await query.edit_message_text(f"@{username} sudah dihapus dari rekap.")


async def handle_delhap_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol hapus/batal dari hasil /hapus (bisa lebih dari 1 lokasi per username)."""
    query = update.callback_query
    await query.answer()

    if query.data == "delhap_batal":
        await query.edit_message_text("Dibatalkan.")
        return

    _, row_index_str, username = query.data.split("|")
    row_index = int(row_index_str)

    try:
        ws = sheets.get_sheet()
        current_row = await asyncio.to_thread(ws.row_values, row_index)
    except Exception as e:
        logger.error("Gagal baca baris sebelum hapus: %s", e)
        await query.edit_message_text("Gagal hapus dari rekap, coba lagi sebentar lagi.")
        return

    # Validasi baris masih username yang sama, jaga-jaga kalau ada baris lain
    # yang sudah lebih dulu dihapus dari tombol lain di pesan yang sama
    # (posisi baris di bawahnya jadi geser).
    current_username = current_row[0].strip().lower() if current_row else ""
    if current_username != username.strip().lower():
        await query.edit_message_text(
            "Data sudah berubah (kemungkinan ada baris lain yang baru dihapus).\n"
            "Jalankan /hapus lagi buat lihat lokasi terbaru."
        )
        return

    try:
        await asyncio.to_thread(sheets.delete_entry, row_index)
    except Exception as e:
        logger.error("Gagal hapus dari Sheets: %s", e)
        await query.edit_message_text("Gagal hapus dari rekap, coba lagi sebentar lagi.")
        return
    await query.edit_message_text(f"@{username} (lokasi ini) sudah dihapus dari rekap.")


# ---------- /lihat: browsing rekap ----------

async def handle_lihat_gallery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, gal_code = query.data.split("|")
    gallery = CODE_TO_GALLERY[gal_code]
    await query.edit_message_text(
        f"Gallery: @{gallery}\nPilih kategori:",
        reply_markup=category_keyboard_lihat(gallery),
    )


async def handle_lihat_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, gal_code, cat_code = query.data.split("|")
    gallery = CODE_TO_GALLERY[gal_code]
    kategori = CODE_TO_CATEGORY[cat_code]

    if kategori in KPOP_CATEGORIES:
        await query.edit_message_text(
            f"Gallery: @{gallery}\nKategori: {kategori}\nPilih grup:",
            reply_markup=group_keyboard_lihat(gallery, kategori, GROUPS),
        )
    else:
        try:
            entries = await asyncio.to_thread(
                sheets.get_entries_by_category, kategori, None, gallery
            )
        except Exception as e:
            logger.error("Gagal baca dari Sheets: %s", e)
            await query.edit_message_text("Gagal ambil rekap, coba lagi sebentar lagi.")
            return
        await query.edit_message_text(
            format_entries(kategori, entries, gallery=gallery),
            reply_markup=back_only_keyboard(f"back|catlihat|{gal_code}"),
        )


async def handle_lihat_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, gal_code, cat_code, grup = query.data.split("|")
    gallery = CODE_TO_GALLERY[gal_code]
    kategori = CODE_TO_CATEGORY[cat_code]
    try:
        entries = await asyncio.to_thread(
            sheets.get_entries_by_category, kategori, grup, gallery
        )
    except Exception as e:
        logger.error("Gagal baca dari Sheets: %s", e)
        await query.edit_message_text("Gagal ambil rekap, coba lagi sebentar lagi.")
        return
    await query.edit_message_text(
        format_entries(kategori, entries, grup, gallery),
        reply_markup=back_only_keyboard(f"back|grplihat|{gal_code}|{cat_code}"),
    )


def format_entries(kategori, entries, grup=None, gallery=None):
    judul = kategori + (f" - {grup}" if grup else "")
    if gallery:
        judul += f" (@{gallery})"
    if not entries:
        return f"{judul}\nBelum ada rekap."

    by_sub: dict[str, list[str]] = {}
    for e in entries:
        # Selalu grouping berdasarkan Nama (member untuk kpop, nama/based-on untuk lainnya).
        # Kolom Grup tidak dipakai lagi di sini karena pada level ini nilainya
        # sudah sama untuk semua entri (grup sudah dipilih sebelumnya).
        sub = e.get("Nama") or "-"
        by_sub.setdefault(sub, []).append(e["Username"])

    lines = [judul]
    for sub, usernames in by_sub.items():
        lines.append(f"\n{sub}")
        lines.extend(f"@{u}" for u in usernames)
    return "\n".join(lines)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "Ada error tak terduga, coba lagi sebentar lagi.",
            )
        except Exception:
            pass


async def post_init(app: Application):
    """Daftarkan daftar command supaya muncul di menu '/' Telegram."""
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Mulai & lihat cara pakai bot"),
            BotCommand("add", "Tambah 1 username manual: /add @username"),
            BotCommand("lihat", "Lihat rekap yang sudah tersimpan"),
            BotCommand("hapus", "Hapus username dari rekap: /hapus @username"),
            BotCommand("hps", "Hapus seluruh list dari satu gallery"),
            BotCommand("group", "Tambah grup & member baru ke daftar"),
        ]
    )


def main():
    persistence = PicklePersistence(filepath="bot_data.pickle")
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .persistence(persistence)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lihat", lihat_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("hapus", hapus_command))
    app.add_handler(CommandHandler("hps", hps_command))
    app.add_handler(CommandHandler("group", group_command))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))

    app.add_handler(CallbackQueryHandler(handle_salah_callback, pattern=r"^salah\|"))
    app.add_handler(CallbackQueryHandler(handle_edit_callback, pattern=r"^edit\|"))
    app.add_handler(CallbackQueryHandler(handle_editbatal_callback, pattern=r"^editbatal\|"))

    # Callback mode "input baru" (index berupa angka)
    app.add_handler(CallbackQueryHandler(handle_category_callback, pattern=r"^cat\|(?!lihat\|)\d"))
    app.add_handler(CallbackQueryHandler(handle_group_callback, pattern=r"^grp\|(?!lihat\|)\d"))
    app.add_handler(CallbackQueryHandler(handle_member_callback, pattern=r"^mem\|"))
    app.add_handler(CallbackQueryHandler(handle_gallery_callback, pattern=r"^gal\|(?!lihat\|)\d"))
    app.add_handler(CallbackQueryHandler(handle_galtxt_callback, pattern=r"^galtxt\|"))
    app.add_handler(CallbackQueryHandler(handle_delete_callback, pattern=r"^del\|"))
    app.add_handler(CallbackQueryHandler(handle_delhap_callback, pattern=r"^delhap"))

    # Callback /hps (hapus seluruh isi satu gallery)
    app.add_handler(CallbackQueryHandler(handle_hps_gallery, pattern=r"^hpsgal\|"))
    app.add_handler(CallbackQueryHandler(handle_hps_confirm, pattern=r"^hpsyes\|"))
    app.add_handler(CallbackQueryHandler(handle_hps_batal, pattern=r"^hpsno$"))

    # Callback /group (tambah grup & member baru)
    app.add_handler(CallbackQueryHandler(handle_ngrp_category, pattern=r"^ngrpcat\|"))
    app.add_handler(CallbackQueryHandler(handle_ngrp_batal, pattern=r"^ngrpbatal$"))

    # Callback mode "/lihat" (gallery -> kategori -> [grup] -> hasil)
    app.add_handler(CallbackQueryHandler(handle_lihat_gallery, pattern=r"^gal\|lihat\|"))
    app.add_handler(CallbackQueryHandler(handle_lihat_category, pattern=r"^cat\|lihat\|"))
    app.add_handler(CallbackQueryHandler(handle_lihat_group, pattern=r"^grp\|lihat\|"))

    # Tombol kembali (semua alur)
    app.add_handler(CallbackQueryHandler(handle_back_callback, pattern=r"^back\|"))

    # Teks biasa paling akhir supaya command lain kepilih duluan
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(error_handler)

    app.run_polling()


if __name__ == "__main__":
    main()
