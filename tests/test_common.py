"""Plain-assert tests (pytest not installed). Run: python tests/test_common.py
download_pdf security: filename sanitising, host allowlist, manual redirects, private-IP rejection."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import common

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n" \
      b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


class FakeResp:
    def __init__(self, status=200, body=PDF, location=None):
        self.status_code, self.body = status, body
        self.headers = {"Location": location} if location else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise common.requests.HTTPError(str(self.status_code))

    def iter_content(self, n):
        yield self.body


class Env:
    """Redirect DOWNLOADS/ROOT to a temp dir, fake requests.get and DNS."""
    def __init__(self, routes, ips=("93.184.216.34",)):
        self.routes, self.ips, self.calls = routes, ips, []

    def __enter__(self):
        self.t = tempfile.TemporaryDirectory()
        self.saved = (common.DOWNLOADS, common.ROOT, common.requests.get, common._resolve_ips)
        root = Path(self.t.name)
        common.ROOT, common.DOWNLOADS = root, root / "downloads"

        def fake_get(url, **kw):
            assert kw.get("allow_redirects") is False, "must follow redirects manually"
            self.calls.append(url)
            return self.routes[url]
        common.requests.get = fake_get
        common._resolve_ips = lambda host: list(self.ips)
        return self

    def __exit__(self, *a):
        common.DOWNLOADS, common.ROOT, common.requests.get, common._resolve_ips = self.saved
        self.t.cleanup()


OK = "https://images.samsung.com/x/manual.pdf"


def test_happy_path_and_filename():
    with Env({OK: FakeResp()}) as e:
        rec = common.download_pdf("Samsung", "RF18A5101SR", "Manual", OK)
        assert rec is not None and rec.pages == 1
        assert (common.ROOT / rec.local_path).is_file()
        assert Path(rec.local_path).as_posix() == "downloads/samsung/RF18A5101SR_Manual.pdf"


def test_model_path_traversal_sanitised():
    with Env({OK: FakeResp()}) as e:
        rec = common.download_pdf("Samsung", "../../evil/..\\x", "Manual", OK)
        assert rec is not None
        p = (common.ROOT / rec.local_path).resolve()
        assert p.is_relative_to(common.DOWNLOADS.resolve()), p
        assert p.parent == (common.DOWNLOADS / "samsung").resolve()
        assert not any(ch in p.name for ch in "/\\")


def test_dotted_model_cannot_be_dotfile():
    with Env({OK: FakeResp()}):
        rec = common.download_pdf("Samsung", "..", "Manual", OK)
        assert rec is None or not Path(rec.local_path).name.startswith(".")


def test_host_allowlist():
    for bad in ("https://evil.example.com/a.pdf", "https://samsung.com.evil.io/a.pdf",
                "https://evilsamsung.com/a.pdf", "http://images.samsung.com/a.pdf",
                "https://user@images.samsung.com/a.pdf", "ftp://images.samsung.com/a.pdf"):
        with Env({bad: FakeResp()}) as e:
            assert common.download_pdf("Samsung", "M1", "Manual", bad) is None, bad
            assert e.calls == [], f"must not fetch {bad}"
    for good in ("https://samsung.com/a.pdf", "https://images.samsung.com/a.pdf", "https://x.geappliances.com/a.pdf",
                 "https://gscs-b2c.lge.com/a.pdf"):
        with Env({good: FakeResp()}):
            assert common.download_pdf("Samsung", "M1", "Manual", good) is not None, good


def test_private_ip_rejected():
    for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "172.16.0.1"):
        with Env({OK: FakeResp()}, ips=(ip,)) as e:
            assert common.download_pdf("Samsung", "M1", "Manual", OK) is None, ip
            assert e.calls == []
    with Env({OK: FakeResp()}, ips=("93.184.216.34", "10.0.0.1")) as e:  # any private answer rejects
        assert common.download_pdf("Samsung", "M1", "Manual", OK) is None


def test_redirect_followed_and_rechecked():
    good = "https://images.samsung.com/final.pdf"
    with Env({OK: FakeResp(302, location=good), good: FakeResp()}) as e:
        assert common.download_pdf("Samsung", "M1", "Manual", OK) is not None
        assert e.calls == [OK, good]
    with Env({OK: FakeResp(302, location="https://evil.example.com/x.pdf")}) as e:
        assert common.download_pdf("Samsung", "M1", "Manual", OK) is None
        assert e.calls == [OK]  # evil hop never requested
    with Env({OK: FakeResp(302, location="http://images.samsung.com/x.pdf")}) as e:
        assert common.download_pdf("Samsung", "M1", "Manual", OK) is None
    with Env({OK: FakeResp(302, location="/relative.pdf"), "https://images.samsung.com/relative.pdf": FakeResp()}) as e:
        assert common.download_pdf("Samsung", "M1", "Manual", OK) is not None


def test_too_many_redirects():
    routes = {f"https://images.samsung.com/{i}.pdf": FakeResp(302, location=f"https://images.samsung.com/{i + 1}.pdf")
              for i in range(10)}
    with Env(routes) as e:
        assert common.download_pdf("Samsung", "M1", "Manual", "https://images.samsung.com/0.pdf") is None
        assert len(e.calls) <= 6


def test_not_pdf_and_size_cap():
    with Env({OK: FakeResp(body=b"<html>nope</html>")}):
        assert common.download_pdf("Samsung", "M1", "Manual", OK) is None
    saved = common.MAX_PDF_BYTES
    common.MAX_PDF_BYTES = 10
    try:
        with Env({OK: FakeResp()}):
            assert common.download_pdf("Samsung", "M1", "Manual", OK) is None
    finally:
        common.MAX_PDF_BYTES = saved


def test_looks_blocked_ignores_markup():
    filler = "<p>" + "hello world " * 40 + "</p>"
    html_with_script = f"<html><head><script>var k='captcha';</script></head><body>{filler}</body></html>"
    assert not common.looks_blocked(200, html_with_script)  # marker only inside script
    assert not common.looks_blocked(200, f'<html><body>{filler}<a href="/captcha-help"></a></body></html>')
    assert common.looks_blocked(403, filler)
    assert common.looks_blocked(200, "")
    assert common.looks_blocked(200, "Access Denied " + "x " * 120)  # visible body text
    assert common.looks_blocked(200, "<html><body><h1>Access Denied</h1>" + "x " * 120 + "</body></html>")



# ------------------------------------------------------------------ download_image
def _img(fmt="PNG", size=(1600, 900), mode="RGB"):
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new(mode, size, (200, 40, 40) if mode == "RGB" else (200, 40, 40, 0)).save(b, fmt)
    return b.getvalue()


IMG_OK = "https://images.samsung.com/x/front.png"


def _stored_images():
    img = common.DOWNLOADS / "images"
    return [f for f in img.rglob("*") if f.is_file()] if img.exists() else []


def test_image_happy_path_downscaled_and_confined():
    from PIL import Image
    with Env({IMG_OK: FakeResp(body=_img("PNG", (1600, 900)))}):
        rel = common.download_image("Samsung", "RF18A5101SR", IMG_OK)
        assert rel is not None and rel.startswith("downloads/images/samsung/RF18A5101SR."), rel
        p = common.ROOT / rel
        assert p.is_file() and p.resolve().is_relative_to((common.DOWNLOADS / "images").resolve())
        with Image.open(p) as im:
            assert im.size == (800, 450), im.size  # longest side 800, aspect ratio kept
        assert not list(p.parent.glob("*.part"))


def test_image_small_not_upscaled_and_formats():
    from PIL import Image
    for fmt, body in (("JPEG", _img("JPEG", (300, 200))), ("WEBP", _img("WEBP", (300, 200)))):
        with Env({IMG_OK: FakeResp(body=body)}):
            rel = common.download_image("Samsung", "M1", IMG_OK)
            assert rel is not None and rel.endswith((".jpg", ".png")), (fmt, rel)
            with Image.open(common.ROOT / rel) as im:
                assert im.size == (300, 200)
    with Env({IMG_OK: FakeResp(body=_img("PNG", (100, 100), "RGBA"))}):  # transparency keeps PNG
        assert common.download_image("Samsung", "M1", IMG_OK).endswith(".png")


def test_image_rejects_bad_content():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    good = _img()
    png_magic = bytes([0x89]) + b"PNG\r\n" + bytes([0x1A]) + b"\n"
    bodies = (b"<html>nope</html>", svg, b"GIF89a" + bytes(50), good[:40], png_magic + b"junk" * 20, b"", PDF)
    for body in bodies:
        with Env({IMG_OK: FakeResp(body=body)}):
            assert common.download_image("Samsung", "M1", IMG_OK) is None, body[:12]
            assert _stored_images() == []  # nothing (not even a .part file) left behind


def test_image_size_cap_and_pixel_cap():
    saved = common.MAX_IMAGE_BYTES
    common.MAX_IMAGE_BYTES = 100
    try:
        with Env({IMG_OK: FakeResp(body=_img("PNG", (800, 800)))}):
            assert common.download_image("Samsung", "M1", IMG_OK) is None
            assert _stored_images() == []
    finally:
        common.MAX_IMAGE_BYTES = saved
    saved = common.MAX_IMAGE_PIXELS
    common.MAX_IMAGE_PIXELS = 1000
    try:
        with Env({IMG_OK: FakeResp(body=_img("PNG", (100, 100)))}):
            assert common.download_image("Samsung", "M1", IMG_OK) is None  # decompression-bomb guard
    finally:
        common.MAX_IMAGE_PIXELS = saved


def test_image_host_allowlist_https_private_ip_redirects():
    body = _img()
    for bad in ("https://evil.example.com/a.png", "https://samsung.com.evil.io/a.png", "http://images.samsung.com/a.png",
                "https://user@images.samsung.com/a.png", "data:image/png;base64,AAAA", "file:///c:/x.png", ""):
        with Env({bad: FakeResp(body=body)}) as e:
            assert common.download_image("Samsung", "M1", bad) is None, bad
            assert e.calls == []
    for good in ("https://image-us.samsung.com/a.png", "https://cdn11.bigcommerce.com/a.png", "https://x.scene7.com/is/image/a",
                 "https://media3.bsh-group.com/a.png", "https://images.salsify.com/a.png", "https://gscs-b2c.lge.com/a.png"):
        with Env({good: FakeResp(body=body)}):
            assert common.download_image("GE", "M1", good) is not None, good
    with Env({IMG_OK: FakeResp(body=body)}, ips=("10.0.0.5",)) as e:
        assert common.download_image("Samsung", "M1", IMG_OK) is None and e.calls == []
    with Env({IMG_OK: FakeResp(302, location="https://evil.example.com/x.png")}) as e:
        assert common.download_image("Samsung", "M1", IMG_OK) is None and e.calls == [IMG_OK]
    good2 = "https://images.samsung.com/final.png"
    with Env({IMG_OK: FakeResp(302, location=good2), good2: FakeResp(body=body)}) as e:
        assert common.download_image("Samsung", "M1", IMG_OK) is not None and e.calls == [IMG_OK, good2]


def test_image_hosts_are_not_pdf_hosts():
    assert not common._host_allowed("cdn11.bigcommerce.com", common.DOWNLOAD_HOST_ALLOW)
    assert common._host_allowed("cdn11.bigcommerce.com", common.IMAGE_HOST_ALLOW)
    assert set(common.DOWNLOAD_HOST_ALLOW) <= set(common.IMAGE_HOST_ALLOW)


def test_image_model_name_cannot_escape():
    with Env({IMG_OK: FakeResp(body=_img())}):
        rel = common.download_image("../../Evil", "../../evil/..\\x", IMG_OK)
        assert rel is not None
        p = (common.ROOT / rel).resolve()
        assert p.is_relative_to((common.DOWNLOADS / "images").resolve()), p
        assert not any(ch in p.name for ch in "/\\") and not p.name.startswith(".")


def test_image_http_errors_and_none_url_return_none():
    with Env({IMG_OK: FakeResp(404, body=b"")}):
        assert common.download_image("Samsung", "M1", IMG_OK) is None
    assert common.download_image("Samsung", "M1", None) is None

def test_image_403_falls_back_to_browser_fetch_with_same_validation():
    url = "https://www.kitchenaid.com/is/image/x?fmt=jpeg&wid=1200"
    calls = []
    saved = common._browser_fetch
    common._browser_fetch = lambda u, allow, cap, label: (calls.append((u, allow, cap)), _img("PNG", (1600, 900)))[1]
    try:
        with Env({url: FakeResp(403, body=b"")}):
            rel = common.download_image("KitchenAid", "KOEC730SWH", url)
            assert rel is not None and calls == [(url, common.IMAGE_HOST_ALLOW, common.MAX_IMAGE_BYTES)]
        calls.clear()
        common._browser_fetch = lambda u, allow, cap, label: (calls.append(u), b"<html>blocked</html>")[1]
        with Env({url: FakeResp(403, body=b"")}):
            assert common.download_image("KitchenAid", "KOEC730SWH", url) is None  # Pillow still validates
            assert _stored_images() == []
        calls.clear()
        with Env({url: FakeResp(404, body=b"")}):  # only 403 triggers the fallback
            assert common.download_image("KitchenAid", "KOEC730SWH", url) is None and calls == []
        with Env({}) as e:  # disallowed host never reaches either path
            assert common.download_image("KitchenAid", "M", "https://evil.example.com/a.jpg") is None and calls == []
    finally:
        common._browser_fetch = saved
    assert common._browser_fetch("https://evil.example.com/a.jpg", common.IMAGE_HOST_ALLOW, 10, "image") is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"{len(fns)} passed")
