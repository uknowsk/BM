import base64, hashlib, io, ipaddress, os, re, socket, sys
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests, fitz  # PyMuPDF
from schema import DocumentRecord

ROOT = Path(__file__).parent
DOWNLOADS = ROOT / "downloads"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
MAX_PDF_BYTES = 60 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000  # decompression-bomb guard, checked on the header before any pixel is decoded
IMAGE_MAX_SIDE = 800
IMAGES_DIR = "images"  # under DOWNLOADS: downloads/images/<brand>/<model>.<jpg|png>


def num(pat: str, text: str, flags: int = 0) -> float | None:
    """First capture group of pat in text as float (thousands separators removed), else None."""
    m = re.search(pat, text, flags)
    return float(m.group(1).replace(",", "")) if m else None


BLOCK_MARKERS = ("access denied", "captcha", "are you a robot", "unusual traffic", "request blocked", "pardon our interruption")


def launch_browser(p, headless: bool | None = None):
    """Launch chromium. headless=None -> env FRIDGE_HEADLESS ('0' = visible window), default headless."""
    if headless is None:
        headless = os.environ.get("FRIDGE_HEADLESS", "1") != "0"
    return p.chromium.launch(headless=headless)


_TAG_BLOCKS = re.compile(r"(?is)<(script|style|noscript|template)\b.*?</\1\s*>|<!--.*?-->")
_TAGS = re.compile(r"(?s)<[^>]*>")


def _visible_text(text: str) -> str:
    """Strip script/style blocks and tags so block markers are only matched against what a user would see."""
    return _TAGS.sub(" ", _TAG_BLOCKS.sub(" ", text))


def looks_blocked(status: int | None, text: str) -> bool:
    """True if a page response looks like bot-blocking (403/429/near-empty body/challenge text).

    `text` should be the page's visible body text (e.g. Playwright inner_text("body")). If raw HTML is
    passed anyway, scripts/styles/tags are stripped before matching BLOCK_MARKERS, so a 'captcha' string in
    an inline script or URL no longer causes a false positive; the near-empty check uses the raw length."""
    raw = (text or "").strip()
    visible = _visible_text(raw).lower() if "<" in raw else raw.lower()
    return status in (403, 429) or len(raw) < 200 or any(m in visible for m in BLOCK_MARKERS)


# Hosts (and their subdomains) PDFs may be downloaded from. Scraped links are untrusted input.
DOWNLOAD_HOST_ALLOW = (
    "samsung.com", "samsungcdn.com", "lg.com", "lge.com", "kitchenaid.com", "whirlpool.com",
    "whirlpoolcorp.com", "bsh-group.com", "bosch-home.com", "geappliances.com", "salsify.com",
    "lge.co.kr",  # Korea: www.lge.co.kr pages/PDFs and static.lge.co.kr product images (not covered by lge.com)
    # Brand expansion (6 -> 30): the sites of the new brands' adapters (server.BRAND_DOMAINS/EXTRA_DOMAINS mirror this;
    # a host an adapter still needs, e.g. its asset CDN, is reported by that adapter's agent and added here).
    "maytag.com", "jennair.com", "amana.com", "thermador.com", "gaggenau.com", "gaggenau.de", "siemens-home.de",
    "frigidaire.com", "electrolux.com", "electroluxappliances.com", "electrolux.de", "electrolux.co.uk", "electrolux.fr",
    "aeg.com", "aeg.de", "aeg.co.uk", "aeg.fr", "cafeappliances.com", "monogram.com", "haierappliances.com", "haier.com",
    "haier-europe.com", "fisherpaykel.com", "fisherpaykel.co.uk", "fisherpaykel.de", "vikingrange.com",
    "vikingrange.co.uk", "subzero-wolf.com", "subzero.com", "wolfappliance.com", "miele.com", "miele.de", "miele.co.uk",
    "miele.fr", "smeg.com", "smegusa.com", "smeg.de", "smeg.co.uk", "smeg.fr", "liebherr.com", "liebherr-home.com",
    "bertazzoni.com", "bertazzoni.co.uk", "dedietrich.com", "dedietrich-electromenager.fr", "beko.com",
    "bekoappliances.com", "beko.co.uk", "beko.de", "hisense-usa.com", "hisense.com", "hisense.co.uk", "hisense.de",
    "panasonic.com", "panasonic.co.uk", "panasonic.de",
    # Brazil (br): national sites of the Brazilian adapters (samsung.com / lg.com / panasonic.com are listed above)
    "electrolux.com.br", "brastemp.com.br", "consul.com.br", "whirlpool.com.br", "bosch-home.com.br",
    "hisense.com.br", "haier.com.br", "smeg.com.br", "miele.com.br",
    # Brazil asset/PDF hosts reported by the adapters (exact hosts only; shared CDNs such as cloudfront.net, s3.amazonaws.com
    # and static.tradesquash.com stay closed, so manuals hosted there are skipped)
    "brastemp.vteximg.com.br", "consul.vteximg.com.br", "electrolux.vteximg.com.br", "whirlpool.vteximg.com.br",
    "electrolux.vtexcrm.com.br", "electrolux-medialibrary.com", "www.electrolux-ui.com",  # Brastemp/Consul/Electrolux BR
    "smegbrasil.com.br", "smegbrasil.cdn.magazord.com.br",  # Smeg BR
    "loja.panasonic.com.br", "panasonic.vtexassets.com", "panasonic.vteximg.com.br", "panasonic-br.zendesk.com",  # Panasonic BR
    "shop.mielebrasil.com.br",  # Miele BR (its image CDN host is added to IMAGE_HOST_ALLOW only)
    # Asset/PDF hosts the adapters reported (exact hosts only: bynder.com / adobeaemcloud.com / windows.net are shared
    # platforms, so only the brands' own tenants are listed).
    "middleby-cdn.com",  # Viking images + PDFs
    "doc.smeg.it", "assets.4flow.cloud",  # Smeg manuals / product images
    "frigidaire.bynder.com", "electrolux.bynder.com", "support.electroluxgroup.eu",  # Frigidaire/Electrolux/AEG assets
)
# Product images: the PDF hosts plus the CDNs the adapters actually emit image URLs from (seen in fixtures/adapters):
# bigcommerce.com (GE hero images, cdn11.bigcommerce.com) and scene7.com (Adobe Scene7, used by Whirlpool-family
# sites). samsung.com covers image-us/images.samsung.com, lge.com covers gscs-b2c.lge.com, bsh-group.com covers
# media3.bsh-group.com, salsify.com covers images.salsify.com. Korea (verified on the live sites): Samsung KR serves
# images from images.samsung.com (samsung.com); LG KR from static.lge.co.kr (lge.co.kr). cloudinary.com is deliberately NOT listed (not needed).
IMAGE_HOST_ALLOW = DOWNLOAD_HOST_ALLOW + (
    "bigcommerce.com", "scene7.com",
    "static.wixstatic.com",  # Hisense product images
    "delivery-p28264-e87620.adobeaemcloud.com",  # Wolf product images (images only, Wolf has no PDFs)
    "d21v6iwzex1yc.cloudfront.net",  # Miele BR product images (this CloudFront distribution only)
)
MAX_REDIRECTS = 5
_REDIRECT_CODES = (301, 302, 303, 307, 308)


def _skip(doc_type: str, url: str, reason: str) -> None:
    print(f"skipped {doc_type} ({url}): {reason}", file=sys.stderr)


def _host_allowed(host: str, allow: tuple[str, ...] = DOWNLOAD_HOST_ALLOW) -> bool:
    return any(host == s or host.endswith("." + s) for s in allow)


def _resolve_ips(host: str) -> list[str]:
    return [ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]


def _url_problem(url: str, allow: tuple[str, ...] = DOWNLOAD_HOST_ALLOW) -> str | None:
    """Reason a download URL must not be fetched (scheme, allowlist, non-public address), else None.
    DNS is resolved here and again by requests, so a rebinding race remains possible; the allowlist is the
    primary control and this check blocks the common loopback/private/link-local targets."""
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
    except ValueError:
        return "invalid url"
    if u.scheme != "https":
        return "non-https url"
    if u.username or u.password or not host:
        return "url has credentials or no host"
    if not _host_allowed(host, allow):
        return f"host {host} not in download allowlist"
    try:
        ips = _resolve_ips(host)
    except OSError as e:
        return f"dns failure ({e})"
    for ip in ips:
        a = ipaddress.ip_address(ip.split("%")[0])
        if not a.is_global or a.is_multicast:
            return f"host resolves to non-public address {a}"
    return None


def _safe_name(value: str, fallback: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value).strip(".") or fallback


def download_pdf(brand: str, model: str, doc_type: str, url: str) -> DocumentRecord | None:
    """Download url into downloads/<brand>/<model>_<doc_type>.pdf; None (logged to stderr) if unusable.
    The URL (and every redirect hop) must be https on DOWNLOAD_HOST_ALLOW and resolve to public addresses."""
    dest_dir = DOWNLOADS / _safe_name(brand, "unknown").lower()
    dest = dest_dir / f"{_safe_name(model, 'unknown')}_{re.sub(r'[^A-Za-z0-9]+', '', doc_type) or 'Doc'}.pdf"
    if not dest.resolve().is_relative_to(DOWNLOADS.resolve()):
        _skip(doc_type, url, "destination escapes downloads dir")
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".pdf.part")
    try:
        cur = url
        for _hop in range(MAX_REDIRECTS + 1):
            problem = _url_problem(cur)
            if problem:
                _skip(doc_type, cur, problem)
                return None
            with requests.get(cur, headers={"User-Agent": UA}, timeout=60, stream=True, allow_redirects=False) as r:
                if r.status_code in _REDIRECT_CODES and r.headers.get("Location"):
                    cur = urljoin(cur, r.headers["Location"])
                    continue
                r.raise_for_status()
                sha, size = hashlib.sha256(), 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        size += len(chunk)
                        if size > MAX_PDF_BYTES:
                            _skip(doc_type, url, f"exceeds {MAX_PDF_BYTES} byte cap")
                            return None
                        sha.update(chunk); f.write(chunk)
            break
        else:
            _skip(doc_type, url, f"more than {MAX_REDIRECTS} redirects")
            return None
        with open(tmp, "rb") as f:
            if f.read(4) != b"%PDF":
                _skip(doc_type, url, "not a PDF")
                return None
        try:
            with fitz.open(tmp) as d: pages = d.page_count
        except Exception as e:  # fitz raises its own FileDataError/RuntimeError types
            _skip(doc_type, url, f"corrupt PDF ({e})")
            return None
        tmp.replace(dest)  # atomic on the same filesystem
    except (requests.RequestException, OSError) as e:
        _skip(doc_type, url, str(e))
        return None
    finally:
        tmp.unlink(missing_ok=True)
    return DocumentRecord(brand=brand, model_number=model, doc_type=doc_type, source_url=url,
                          local_path=str(dest.relative_to(ROOT)), sha256=sha.hexdigest(),
                          size_bytes=size, pages=pages)


_PAGE_FETCH_JS = """async ({u, cap}) => {
  const r = await fetch(u, {redirect: 'manual', credentials: 'include'});
  if (r.type === 'opaqueredirect') return {s: 'redirect'};
  if (!r.ok) return {s: r.status};
  const b = await r.blob();
  if (b.size > cap) return {s: 'too big'};
  const d = await new Promise((res) => { const f = new FileReader(); f.onload = () => res(f.result); f.readAsDataURL(b); });
  return {s: 200, d: d.split(',')[1]};
}"""


def _browser_fetch(url: str, allow: tuple[str, ...], cap: int, label: str) -> bytes | None:
    """Fallback for CDNs (Akamai: KitchenAid/Whirlpool) that answer plain requests with 403: fetch through a real
    headless Chromium (page.request shares the browser's TLS fingerprint and cookies). Same rules as _fetch_capped:
    https + allowlist + public IP check on the URL (redirects are not followed), size capped."""
    problem = _url_problem(url, allow)  # before any browser start / origin visit
    if problem:
        _skip(label, url, problem)
        return None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chromium", headless=True)
            try:
                ctx = browser.new_context(user_agent=UA)
                page = ctx.new_page()
                host = (urlparse(url).hostname or "").lower()
                origin = "https://www." + ".".join(host.split(".")[-2:]) + "/"
                try:  # visit the brand origin first so the CDN's cookies/challenge are set
                    page.goto(origin, wait_until="domcontentloaded", timeout=30000)
                except Exception:  # noqa: BLE001 - the image request may still work
                    pass
                # page.request still gets 403 from Akamai; an in-page fetch() (same TLS/cookies as the page) works.
                # Redirects are not followed ('manual'), so no hop can leave the allowlist unchecked.
                res = page.evaluate(_PAGE_FETCH_JS, {"u": url, "cap": cap})
                if res.get("s") != 200:
                    _skip(label, url, f"browser fetch failed ({res.get('s')})")
                    return None
                return base64.b64decode(res["d"])
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 - playwright missing / launch failure: just no image
        _skip(label, url, f"browser fallback failed ({type(e).__name__})")
    return None


def _fetch_capped(url: str, allow: tuple[str, ...], cap: int, label: str, browser_fallback: bool = False) -> bytes | None:
    """GET url into memory (https + allowlist + public IPs re-checked on every redirect hop, size capped).
    With browser_fallback, an HTTP 403 retries that hop through _browser_fetch."""
    cur = url
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            problem = _url_problem(cur, allow)
            if problem:
                _skip(label, cur, problem)
                return None
            with requests.get(cur, headers={"User-Agent": UA}, timeout=30, stream=True, allow_redirects=False) as r:
                if r.status_code in _REDIRECT_CODES and r.headers.get("Location"):
                    cur = urljoin(cur, r.headers["Location"])
                    continue
                if r.status_code == 403 and browser_fallback:
                    return _browser_fetch(cur, allow, cap, label)
                r.raise_for_status()
                buf = bytearray()
                for chunk in r.iter_content(1 << 16):
                    buf += chunk
                    if len(buf) > cap:
                        _skip(label, url, f"exceeds {cap} byte cap")
                        return None
                return bytes(buf)
        _skip(label, url, f"more than {MAX_REDIRECTS} redirects")
    except (requests.RequestException, OSError) as e:
        _skip(label, url, str(e))
    return None


_IMG_MAGIC = ((bytes([0xFF, 0xD8, 0xFF]), "JPEG"), (bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]), "PNG"))


def _sniff_image(data: bytes) -> str | None:
    for magic, fmt in _IMG_MAGIC:
        if data.startswith(magic):
            return fmt
    return "WEBP" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None


def _normalize_image(data: bytes) -> tuple[bytes, str] | None:
    """Fully decode with Pillow, downscale so the longest side <= IMAGE_MAX_SIDE, re-encode (JPEG, or PNG when the
    source has transparency). Re-encoding also drops any embedded metadata/payload. None if not a valid image."""
    from PIL import Image
    fmt = _sniff_image(data)
    if fmt is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format != fmt or im.size[0] * im.size[1] > MAX_IMAGE_PIXELS:
                return None
            im.load()  # raises on truncated/corrupt data
            alpha = im.mode in ("RGBA", "LA") or "transparency" in im.info
            im = im.convert("RGBA" if alpha else "RGB")
            im.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE), Image.LANCZOS)  # never upscales
            out = io.BytesIO()
            if alpha:
                im.save(out, "PNG", optimize=True)
            else:
                im.save(out, "JPEG", quality=85, optimize=True)
            return out.getvalue(), "png" if alpha else "jpg"
    except Exception:  # noqa: BLE001 - Pillow raises many types (UnidentifiedImageError, OSError, SyntaxError, ...)
        return None


def download_image(brand: str, model: str, url: str | None) -> str | None:
    """Download a product image into downloads/images/<brand>/<model>.<jpg|png>; path relative to the project root,
    or None (reason logged to stderr) when unusable. https only, every hop on IMAGE_HOST_ALLOW and a public address,
    max MAX_IMAGE_BYTES, magic bytes AND a full Pillow decode must agree, longest side downscaled to 800 px."""
    if not url:
        return None
    try:
        import PIL  # noqa: F401
    except ImportError:
        _skip("image", url, "Pillow not installed")
        return None
    base = (DOWNLOADS / IMAGES_DIR).resolve()
    dest_dir = DOWNLOADS / IMAGES_DIR / _safe_name(brand, "unknown").lower()
    stem = _safe_name(model, "unknown")
    if not (dest_dir / stem).resolve().is_relative_to(base):
        _skip("image", url, "destination escapes images dir")
        return None
    data = _fetch_capped(url, IMAGE_HOST_ALLOW, MAX_IMAGE_BYTES, "image", browser_fallback=True)
    if data is None:
        return None
    done = _normalize_image(data)
    if done is None:
        _skip("image", url, "not a valid jpg/png/webp image")
        return None
    body, ext = done
    dest = dest_dir / f"{stem}.{ext}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(body)
        for other in dest_dir.glob(f"{stem}.*"):  # a previous download of this model in the other format
            if other != tmp and other.suffix.lower() in (".jpg", ".png") and other.name != dest.name:
                other.unlink(missing_ok=True)
        tmp.replace(dest)  # atomic on the same filesystem
    except OSError as e:
        _skip("image", url, str(e))
        return None
    finally:
        tmp.unlink(missing_ok=True)
    return str(dest.relative_to(ROOT)).replace(os.sep, "/")


def pdf_text(path: str | Path) -> str:
    with fitz.open(path) as d: return "\n".join(p.get_text() for p in d)
