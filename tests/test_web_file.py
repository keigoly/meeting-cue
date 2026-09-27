"""WebUI のファイル配信: <audio> のシークに要る Range(206)。"""
import socket
import urllib.request

from meetcue.ui.web import WebUI


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_range_requests(tmp_path):
    f = tmp_path / "a.m4a"
    f.write_bytes(bytes(range(256)) * 4)   # 1024 bytes
    ui = WebUI(port=_port())
    ui.file_route = lambda path: (f, "audio/mp4") if path == "/x/a.m4a" else None
    ui.start()
    try:
        base = f"http://127.0.0.1:{ui.port}/x/a.m4a"
        r = urllib.request.urlopen(base)
        assert r.status == 200 and len(r.read()) == 1024 and r.headers["Accept-Ranges"] == "bytes"
        r = urllib.request.urlopen(urllib.request.Request(base, headers={"Range": "bytes=10-19"}))
        assert r.status == 206 and r.headers["Content-Range"] == "bytes 10-19/1024" and r.read() == bytes(range(10, 20))
        r = urllib.request.urlopen(urllib.request.Request(base, headers={"Range": "bytes=1000-"}))
        assert r.status == 206 and len(r.read()) == 24
        r = urllib.request.urlopen(urllib.request.Request(base, headers={"Range": "bytes=-4"}))
        assert r.read() == bytes([252, 253, 254, 255])
        try:
            urllib.request.urlopen(urllib.request.Request(base, headers={"Range": "bytes=5000-"}))
            raise AssertionError("416 expected")
        except urllib.error.HTTPError as e:
            assert e.code == 416
    finally:
        ui.stop()


def test_nagi_images_only_known_moods():
    """ナギの絵(2026-09-27): 決まった表情の名前だけ返す・届いていない表情は待機中の絵・それ以外は 404。"""
    from meetcue.ui.web import STATIC
    idle = (STATIC / "nagi" / "idle.png").read_bytes()
    ui = WebUI(port=_port())
    ui.start()
    try:
        base = f"http://127.0.0.1:{ui.port}"
        r = urllib.request.urlopen(base + "/nagi/idle.png")
        assert r.status == 200 and r.headers["Content-Type"] == "image/png" and r.read() == idle
        for mood in ("listen", "cue", "think", "done", "alert"):   # 絵が無ければ idle.png で代える
            f = STATIC / "nagi" / f"{mood}.png"
            assert urllib.request.urlopen(base + f"/nagi/{mood}.png").read() == (f.read_bytes() if f.exists() else idle)
        for bad in ("/nagi/secret.png", "/nagi/../app.html", "/nagi/idle.jpg", "/nagi/IDLE.png"):
            try:
                urllib.request.urlopen(base + bad)
                raise AssertionError(f"404 expected: {bad}")
            except urllib.error.HTTPError as e:
                assert e.code == 404
    finally:
        ui.stop()
