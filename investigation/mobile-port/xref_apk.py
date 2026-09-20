"""Method-level xref analysis: who actually calls AudioRecord / MediaRecorder.

Extracts dexes containing the markers from an APK, builds an xref analysis per
dex, and prints caller classes/methods that invoke recording APIs.
"""
import sys, zipfile, io
from androguard.core.dex import DEX
from androguard.core.analysis.analysis import Analysis

TARGETS = {
    "Landroid/media/AudioRecord;": "AudioRecord",
    "Landroid/media/MediaRecorder;": "MediaRecorder",
}
KEY_HINTS = ("ptt", "voip", "gvoice", "wxaudio", "voice", "audio", "record", "silk", "amr")

def analyze_dex(name: str, data: bytes) -> None:
    try:
        d = DEX(data)
        dx = Analysis(d)
        dx.create_xref()
    except Exception as e:
        print(f"  [{name}] analysis failed: {e}")
        return

    rows = []
    for ext_cls, short in TARGETS.items():
        for m in dx.find_methods(classname=f"^{ext_cls.replace('.', '/')}$"):
            # external class method; callers via xref_from
            try:
                xref_from = m.get_xref_from()
            except Exception:
                xref_from = []
            for cls_analysis, caller_m, _off in xref_from:
                caller_cls = str(caller_m.class_name)
                rows.append((short, caller_cls, caller_m.name, m.method.name if hasattr(m, "method") else str(m)))

    if not rows:
        print(f"  [{name}] no direct callers found")
        return

    print(f"  [{name}] callers:")
    seen = set()
    for short, cls, meth, api in sorted(set(rows)):
        if (cls, short) in seen:
            continue
        seen.add((cls, short))
        flag = " *" if any(k in cls.lower() for k in KEY_HINTS) else ""
        print(f"    {short:14s} <- {cls}{flag}  (e.g. .{meth} -> {api})")

def main(apk_path: str) -> None:
    print(f"\n===== {apk_path} =====")
    z = zipfile.ZipFile(apk_path)
    dexes = []
    for n in z.namelist():
        if n.endswith(".dex"):
            data = z.read(n)
            if b"android/media/AudioRecord" in data or b"android/media/MediaRecorder" in data:
                dexes.append((n, data))
    print(f"dexes with markers: {[n for n, _ in dexes]}")
    for n, data in dexes:
        analyze_dex(n, data)

if __name__ == "__main__":
    for p in sys.argv[1:]:
        main(p)
