import os
import re
from io import BytesIO

import pytesseract
from PIL import Image, ImageOps, ImageStat

# Di Windows, kalau Tesseract tidak ada di PATH, isi TESSERACT_CMD di .env, mis.
# TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
_cmd = os.environ.get("TESSERACT_CMD")
if _cmd:
    pytesseract.pytesseract.tesseract_cmd = _cmd

# Ambil username dari 2 kemungkinan pola:
# 1. "t.me/<username>"            -> screenshot Telegram biasa
# 2. "/<username>" berdiri sendiri -> panel "Channel Link" Telegram X
USERNAME_PATTERN = re.compile(
    r"(?:t\.me/|(?<!\S)/)([A-Za-z][A-Za-z0-9_]{4,31})",
    re.IGNORECASE,
)

# Batasi karakter yang boleh dibaca supaya Tesseract tidak "mengarang" simbol aneh.
_WHITELIST = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./@"

# Beberapa mode pembacaan. Hasil pass pertama jadi patokan urutan, pass berikutnya
# hanya menambah username yang BELUM ketemu (untuk mengurangi yang terlewat).
_CONFIGS = [
    f"--oem 3 --psm 6 -c tessedit_char_whitelist={_WHITELIST}",   # blok teks
    f"--oem 3 --psm 11 -c tessedit_char_whitelist={_WHITELIST}",  # teks tersebar
]


def _prepare(image: Image.Image) -> Image.Image:
    """Perjelas gambar: grayscale, balik kalau dark mode, perbesar, naikkan kontras."""
    image = ImageOps.exif_transpose(image).convert("L")

    # Screenshot dark mode (teks terang di latar gelap) sulit dibaca Tesseract.
    if ImageStat.Stat(image).mean[0] < 128:
        image = ImageOps.invert(image)

    # Huruf kecil di screenshot HP bikin i/I/l dan q/g tertukar -> perbesar.
    w, h = image.size
    longest = max(w, h)
    scale = 3 if longest < 1500 else 2 if longest < 2500 else 1
    if scale > 1:
        image = image.resize((w * scale, h * scale), Image.LANCZOS)

    return ImageOps.autocontrast(image)


def _fuzzy_key(username: str) -> str:
    """Kunci pembanding untuk mendeteksi 'username yang sama tapi salah baca'
    antar pass OCR (i/I/l/1 dianggap sama, q/g sama, o/0 sama)."""
    k = username.lower()
    for ch in "l1":
        k = k.replace(ch, "i")
    k = k.replace("q", "g").replace("0", "o")
    return k


def extract_usernames_from_image(image_bytes: bytes) -> list[str]:
    """Baca teks di gambar pakai Tesseract, ambil yang berpola
    't.me/<username>' atau '/<username>'.

    Fungsi sinkron/blocking, dipanggil lewat asyncio.to_thread() di bot.py.
    """
    image = _prepare(Image.open(BytesIO(image_bytes)))

    usernames: list[str] = []
    seen_exact: set[str] = set()
    seen_fuzzy: set[str] = set()

    for pass_no, config in enumerate(_CONFIGS):
        text = pytesseract.image_to_string(image, config=config)
        for u in USERNAME_PATTERN.findall(text):
            exact = u.lower()
            if exact in seen_exact:
                continue
            # Pass ke-2 dst: lewati kalau cuma beda ejaan ambigu dari yang sudah ada
            # (misal "llya" vs "Ilya"), supaya tidak jadi dobel.
            if pass_no > 0 and _fuzzy_key(u) in seen_fuzzy:
                continue
            seen_exact.add(exact)
            seen_fuzzy.add(_fuzzy_key(u))
            usernames.append(u)

    return usernames
