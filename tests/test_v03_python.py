"""Pruebas de los módulos nuevos de v0.3 (python -m pytest tests/test_v03_python.py)."""
import datetime as dt
import json
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocalchain.keyparse import parse_key_text, KeyInfo
from vocalchain import recording as R
from vocalchain.voice_profiles import PROFILES, EQ_PRE, EQ_POST, AUTOTUNE, apply_profile, resolve_param
from vocalchain.latency import chain_report, PluginLatency


def test_parse_key_text():
    cases = {
        "A Minor": (9, True), "Am": (9, True), "A min": (9, True), "A#m": (10, True),
        "Bb Major": (10, False), "C# minor": (1, True), "F♯ Maj": (6, False), "A Minor (Aeolian)": (9, True),
        "La menor": (9, True), "Do# mayor": (1, False), "Sol m": (7, True), "Sib": (10, False),
        "Ebm": (3, True), "E Minor": (4, True), "B Minor": (11, True), "Bm": (11, True), "B": (11, False),
        "G": (7, False), "D/Minor": (2, True), "F# Minor": (6, True), "Mi menor": (4, True), "Re": (2, False),
        "c minor": (0, True), "Ab": (8, False), "Gb Major": (6, False), "B Flat Minor": (10, True),
    }
    for txt, (root, minor) in cases.items():
        k = parse_key_text(txt)
        assert k == KeyInfo(root, minor), (txt, k)
    for txt in ["Chromatic", "", None, "---", "Detecting...", "Listening"]:
        assert parse_key_text(txt) is None, txt
    assert parse_key_text("Am").relative() == KeyInfo(0, False)
    assert parse_key_text("A Minor").name == "A Minor"
    assert parse_key_text("Bb").name == "Bb Major"


def _write_wav_f32(p: Path, sr=48000, secs=2.0, ch=2, extensible=True):
    n = int(sr * secs)
    data = b"".join(struct.pack("<" + "f" * ch, *([0.5 * math.sin(2 * math.pi * 440 * i / sr)] * ch))
                    for i in range(n))
    if extensible:
        fmt = struct.pack("<HHIIHH", 0xFFFE, ch, sr, sr * ch * 4, ch * 4, 32) + struct.pack("<HHI", 22, 32, 3) \
              + struct.pack("<H", 3) + b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
    else:
        fmt = struct.pack("<HHIIHH", 3, ch, sr, sr * ch * 4, ch * 4, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    p.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def test_wav_to_mp3(tmp_path):
    for ext in (True, False):
        w = tmp_path / f"t{ext}.wav"
        _write_wav_f32(w, extensible=ext)
        assert abs(R.wav_duration(w) - 2.0) < 1e-6
        mp3 = R.wav_to_mp3(w, tmp_path / f"t{ext}.mp3")
        b = mp3.read_bytes()
        assert len(b) > 20000 and (b[:3] == b"ID3" or b[0] == 0xFF)


def test_recording_song_and_session(tmp_path):
    now = dt.datetime(2026, 10, 7, 21, 30)
    rec = R.Recording.start("song", instrumental='Beat "Noche": trap/oscuro?', youtube_url="https://youtu.be/x",
                            key="A Minor", preset="Trap duro", chain=["Auto-Tune Pro", "Pro-Q 4"],
                            root=tmp_path, now=now)
    assert rec.folder.name == "Beat Noche trap oscuro - 2026-10-07 21-30"
    _write_wav_f32(rec.wav_tmp, secs=1.0)
    rec.finish(blocking=True)
    assert rec.error is None and rec.mp3_path.exists() and not rec.wav_tmp.exists()
    info = json.loads((rec.folder / "info.json").read_text(encoding="utf-8"))
    assert info["key"] == "A Minor" and abs(info["duration_s"] - 1.0) < 0.01
    assert (rec.folder / "letra.txt").exists() and "Trap duro" in (rec.folder / "info.txt").read_text("utf-8")
    rec2 = R.Recording.start("song", instrumental='Beat "Noche": trap/oscuro?', root=tmp_path, now=now)
    assert rec2.folder.name.endswith("(2)")
    s = R.Recording.start("session", instrumental="Beat 1", key="Am", root=tmp_path, now=now)
    s.add_marker(95.4, "Beat 2", "https://youtu.be/y", "C# Minor")
    _write_wav_f32(s.wav_tmp, secs=0.5)
    s.finish(engine_markers=[{"t": 95.37, "name": "Beat 2"}], blocking=True)
    txt = (s.folder / "info.txt").read_text("utf-8")
    assert "1:35" in txt and "Beat 1" in txt and "Beat 2" in txt
    assert R.read_info(s.folder).markers[1].t == 95.37
    assert len(R.list_recordings(tmp_path)) == 3


def test_failed_conversion_keeps_wav(tmp_path):
    rec = R.Recording.start("song", instrumental="x", root=tmp_path)
    rec.wav_tmp.write_bytes(b"basura")
    rec.finish(duration_s=3, blocking=True)
    assert rec.error and rec.wav_tmp.exists()
    assert R.read_info(rec.folder).audio_file == rec.wav_tmp.name


def test_youtube_helpers():
    assert R.normalize_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RD&t=3") \
        == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert R.normalize_youtube_url("youtu.be/dQw4w9WgXcQ?si=a").endswith("dQw4w9WgXcQ")
    assert R.normalize_youtube_url("hola") is None
    assert R.clean_title_for_search('(3) Beat Trap "Noche" (Prod. X) - YouTube — Mozilla Firefox') \
        == "Beat Trap Noche (Prod. X)"


def _proq_names(bands=24):
    per = ["Used", "Enabled", "Frequency", "Gain", "Q", "Shape", "Slope", "Stereo Placement", "Speakers",
           "Dynamic Range", "Dynamics Enabled", "Dynamics Auto", "Threshold", "Attack", "Release",
           "External Side Chain", "Side Chain Filtering", "Side Chain Low Frequency", "Side Chain High Frequency",
           "Side Chain Audition", "Spectral Enabled", "Spectral Density", "Solo"]
    return [f"Band {b} {p}" for b in range(1, bands + 1) for p in per]


def test_profiles():
    at_params = ["Correction Mode", "Scale", "Key", "x", "Retune Speed"] + [f"p{i}" for i in range(5, 80)]
    at_params[9], at_params[10], at_params[61], at_params[62], at_params[70] = \
        "Tracking", "Input Type", "Humanize", "Natural Vibrato", "Formant Correction"
    assert resolve_param(at_params, ("Retune Speed", 4)) == 4
    assert resolve_param(at_params, ("humanize", 61)) == 61
    sent = []
    w = apply_profile(PROFILES[0], {AUTOTUNE: (1, at_params)}, lambda s, i, t: sent.append((s, i, t)) or True)
    assert (1, 4, "3") in sent and (1, 10, "Alto-Tenor") in sent
    assert any(EQ_PRE in x for x in w) and any(EQ_POST in x for x in w)
    # con los dos Pro-Q: todo encontrado, sin avisos
    pq = _proq_names()
    sent = []
    w = apply_profile(PROFILES[1], {AUTOTUNE: (1, at_params), EQ_PRE: (2, pq), EQ_POST: (3, pq)},
                      lambda s, i, t: sent.append((s, i, t)) or True)
    assert w == [], w
    assert (2, pq.index("Band 1 Shape"), "Low Cut") in sent
    assert (3, pq.index("Band 4 Dynamics Enabled"), "Dynamics Enabled") in sent
    assert [p.name for p in PROFILES] == ["Trap duro", "Melódico", "Natural / freestyle"]


def test_latency_report():
    r = chain_report([PluginLatency("Auto-Tune Pro", 2670), PluginLatency("Pro-Q 4", 0)], 44100, 16,
                     io_latency_ms=5.1, pc_sr=48000)
    assert r["plugins_ms"] == 60.5 and r["total_ms"] == 65.6
    assert any("Low Latency" in h for h in r["hints"]) and any("48 kHz" in h for h in r["hints"])
    assert any("Buffer 16" in h for h in r["hints"])


def test_remote_autokey_text():
    pytest = __import__("pytest")
    pytest.importorskip("PySide6")
    from vocalchain.remote import parse_key_text as pk
    assert pk("A Minor").root == 9 and pk("A Minor").mode == "minor"
    assert pk("Chromatic") is None
    assert pk("C#", "Minor").mode == "minor"
