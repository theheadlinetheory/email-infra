"""A client's logo as a mailbox profile picture.

The cold-calling contact@ inboxes show the client's logo, not Sean's headshot
(Tim, 2026-10-27). The seven made by hand live in the public `headshots` bucket
as `client_logos/<slug>.png`: the logo from the client's site, centred on a
512x512 square padded with its own background colour. `ensure()` reuses that
file when it exists — so a logo dropped in by hand always wins — and otherwise
builds one the same way from the website.
"""
from __future__ import annotations

import io as _io
import re
from urllib.parse import urljoin, urlparse

import requests

import db

SIZE = 512
FILL = 0.78                 # the logo's longest side, as a share of the square
BUCKET = "headshots"
FOLDER = "client_logos"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def slug_for(brand: str) -> str:
    """'Woody's' -> 'woodys', 'Mighty Oak' -> 'mightyoak' (the bucket's names)."""
    return re.sub(r"[^a-z0-9]+", "", (brand or "").lower())


def public_url(slug: str) -> str:
    return f"{db.SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{FOLDER}/{slug}.png"


def _exists(url: str, get=requests.head) -> bool:
    try:
        return get(url, timeout=10).status_code == 200
    except Exception:                                # noqa: BLE001
        return False


_OTHERS = re.compile(r"client|partner|sponsor|brands?[-_/]|award|certif|badge|review", re.I)


def logo_candidates(html: str, base: str, brand: list[str] | None = None) -> list[str]:
    """Most logo-like first. A page carries other people's logos too (a
    "clients" strip put A&W ahead of Greenblock's own), so each candidate is
    scored: naming the client beats everything, then "logo" in the page
    <header>, the site's declared schema.org logo, a logo-named og:image, the
    apple-touch-icon; logos under client/partner paths sink to the bottom.
    Last resort is the first image on the page — site builders (Woody's, on
    Duda) mark the logo up as nothing at all. SVGs are skipped: nothing here
    can rasterise them."""
    brand = [b for b in (brand or []) if len(b) >= 3]
    header = re.search(r"<header\b.*?</header>", html, re.I | re.S)
    hspan = header.span() if header else (0, 0)
    found = {}                                    # url -> best score

    def words(tag):
        """What an <img> says about itself: alt and title only. Its srcset
        repeats the site's own domain, which would "name" every image."""
        return " ".join(re.findall(r"""\b(?:alt|title)\s*=\s*["']([^"']*)""", tag, re.I))

    def add(u, text, score):
        if not u or u.startswith("data:"):
            return
        full = urljoin(base, u.strip())
        if re.search(r"\.svg(\?|$)", full, re.I):
            return
        # Brand from the path and the tag only: every URL on the client's own
        # site carries their domain, so the host would make everything "named".
        path = urlparse(full).path
        flat = re.sub(r"[^a-z0-9]+", "", (text + " " + path).lower())
        if any(b in flat for b in brand):
            score += 100
        if _OTHERS.search(path):
            score -= 80
        if re.search(r"icon|favicon", path, re.I):
            score -= 30          # a site icon is a fallback, not the wordmark
        found[full] = max(score, found.get(full, -999))

    first_img = None
    for m in re.finditer(r"<img\b[^>]*>", html, re.I):
        tag = m.group(0)
        src = re.search(r"""\b(?:data-src|src)\s*=\s*["']([^"']+)""", tag, re.I)
        if not src:
            continue
        in_header = hspan[0] <= m.start() < hspan[1]
        if re.search(r"logo", tag, re.I):
            add(src.group(1), words(tag) + (" logo" if "logo" in tag.lower() else ""), 60 if in_header else 30)
        else:
            # Timesavers' wordmark is "Time-Savers_Red-and-White.png": no
            # "logo" anywhere, only the brand. add() scores the brand.
            named = re.sub(r"[^a-z0-9]+", "", (words(tag) + urlparse(src.group(1)).path).lower())
            if any(b in named for b in brand):
                add(src.group(1), words(tag), 40 if in_header else 20)
            if first_img is None:
                first_img = (src.group(1), words(tag))
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.I | re.S):
        for lm in re.finditer(r'"logo"\s*:\s*(?:\{[^}]*?"url"\s*:\s*)?"([^"]+)"', m.group(1)):
            add(lm.group(1).replace("\\/", "/"), "", 50)
    for m in re.finditer(r"<(?:link|meta)\b[^>]*>", html, re.I):
        tag = m.group(0)
        href = re.search(r"""\b(?:href|content)\s*=\s*["']([^"']+)""", tag, re.I)
        if not href:
            continue
        low = tag.lower()
        if "apple-touch-icon" in low:
            add(href.group(1), "", 35)
        elif re.search(r"""og:image["']""", low):
            add(href.group(1), "", 45 if "logo" in href.group(1).lower() else 5)
    if first_img:
        add(first_img[0], first_img[1], 10)
    return sorted(found, key=lambda u: -found[u])


def square_png(raw: bytes) -> bytes:
    """Centre the logo on a 512 square, padded with its own background."""
    from PIL import Image
    im = Image.open(_io.BytesIO(raw))
    im.load()
    im = im.convert("RGBA")
    bbox = im.getchannel("A").getbbox()
    if bbox:
        im = im.crop(bbox)
    w, h = im.size
    alpha = im.getchannel("A")
    if alpha.getextrema()[0] < 250:
        # Transparent logo: white behind it, unless the logo itself is light
        # (a white wordmark on a dark site), then the dark slate Timesavers uses.
        # Timesavers' is white type and a red arrow: average brightness says
        # "dark logo" while the type vanishes on white. Count the white instead.
        px = [p for p in im.getdata() if p[3] > 128]
        light = sum(1 for r, g, b, _ in px if 0.299 * r + 0.587 * g + 0.114 * b > 215)
        bg = (31, 42, 55) if light > 0.3 * max(1, len(px)) else (255, 255, 255)
    else:
        border = [im.getpixel((x, y)) for x in range(w) for y in (0, h - 1)] + \
                 [im.getpixel((x, y)) for y in range(h) for x in (0, w - 1)]
        bg = max(set(p[:3] for p in border), key=lambda c: sum(1 for p in border if p[:3] == c))
    scale = SIZE * FILL / max(w, h)
    im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    canvas = Image.new("RGB", (SIZE, SIZE), bg)
    canvas.paste(im, ((SIZE - im.size[0]) // 2, (SIZE - im.size[1]) // 2), im)
    out = _io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()


def _upload(slug: str, png: bytes) -> None:
    r = requests.post(
        f"{db.SUPABASE_URL}/storage/v1/object/{BUCKET}/{FOLDER}/{slug}.png",
        headers={"apikey": db.SUPABASE_KEY, "Authorization": f"Bearer {db.SUPABASE_KEY}",
                 "Content-Type": "image/png", "x-upsert": "true"},
        data=png, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"logo upload refused: {r.status_code} {r.text[:120]}")


def _is_photo(im) -> bool:
    """A photograph, not a logo: thousands of colours and no transparency.
    Galaxy's page offered a photo of a person; a wrong face on a client's
    inbox is worse than none."""
    if "A" in im.getbands() or im.mode == "P":
        return False
    small = im.convert("RGB")
    small.thumbnail((128, 128))
    return small.getcolors(maxcolors=3000) is None


def find_logo(website: str, brand: list[str] | None = None, get=requests.get) -> bytes | None:
    """The first candidate on the site that is a real raster image of a usable size."""
    site = website if re.match(r"^https?://", website or "", re.I) else f"https://{website}"
    try:
        r = get(site, headers={"User-Agent": UA}, timeout=15)
    except requests.exceptions.SSLError:
        # gmlandscapingoh.com's certificate only covers www.
        host = urlparse(site).netloc
        if host.startswith("www."):
            raise
        site = site.replace(host, "www." + host, 1)
        r = get(site, headers={"User-Agent": UA}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"{site} answered {r.status_code}")
    from PIL import Image
    for url in logo_candidates(r.text, r.url or site, brand):
        try:
            img = get(url, headers={"User-Agent": UA, "Referer": site}, timeout=15)
            if img.status_code != 200 or not img.content:
                continue
            im = Image.open(_io.BytesIO(img.content))
            if max(im.size) < 100 or _is_photo(im):
                continue
            return img.content
        except Exception:                            # noqa: BLE001 — try the next candidate
            continue
    return None


def ensure(logo: dict) -> dict:
    """{url} of the client's logo in the bucket, building it if need be; else {error}."""
    slug = slug_for(logo.get("slug") or "")
    if not slug:
        return {"error": "no logo slug on the order"}
    url = public_url(slug)
    if _exists(url):
        return {"url": url}
    site = logo.get("website") or ""
    try:
        raw = find_logo(site, brand=logo.get("brand") or [])
        if not raw:
            return {"error": f"no logo found on {site} — put a square PNG at "
                             f"{BUCKET}/{FOLDER}/{slug}.png and it is picked up on the next pass"}
        _upload(slug, square_png(raw))
    except Exception as e:                           # noqa: BLE001
        return {"error": f"logo from {site}: {str(e)[:120]} — or put a PNG at "
                         f"{BUCKET}/{FOLDER}/{slug}.png"}
    return {"url": url}
