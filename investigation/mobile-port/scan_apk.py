"""Scan APK native libs + dex for audio-recording API usage (static evidence).

Markers:
  libOpenSLES.so / slCreateEngine      -> OpenSL ES native recording (C)
  libaaudio.so / AAudioStream_read     -> AAudio native recording (C)
  android/media/AudioRecord            -> Java/JNI AudioRecord usage
  android/media/MediaRecorder          -> Java MediaRecorder
  libaudioclient_android.so refs etc.
"""
import sys, zipfile, re

MARKERS = [
    (b"libOpenSLES.so", "OpenSL ES (DT_NEEDED)"),
    (b"slCreateEngine", "OpenSL ES slCreateEngine"),
    (b"slRecorderRealize", "OpenSL ES recorder"),
    (b"libaaudio.so", "AAudio (DT_NEEDED)"),
    (b"AAudioStream_read", "AAudioStream_read"),
    (b"AAudioStream_requestStart", "AAudioStream_requestStart"),
    (b"android/media/AudioRecord", "JNI ref: android/media/AudioRecord"),
    (b"android/media/MediaRecorder", "JNI ref: MediaRecorder"),
    (b"readAudioFrame", "custom: readAudioFrame"),
    (b"SLES", "SLES string"),
]

def scan(apk_path: str) -> None:
    print(f"\n===== {apk_path} =====")
    z = zipfile.ZipFile(apk_path)
    so_files = [n for n in z.namelist() if n.startswith("lib/arm64-v8a/") and n.endswith(".so")]
    print(f"native libs (arm64-v8a): {len(so_files)}")

    dex_names = [n for n in z.namelist() if n.endswith(".dex")]
    # AudioRecord Java-class references in dex (weak signal, count only)
    for dn in dex_names:
        data = z.read(dn)
        n_rec = data.count(b"android/media/AudioRecord")
        n_med = data.count(b"android/media/MediaRecorder")
        if n_rec or n_med:
            print(f"  [dex] {dn}: AudioRecord refs={n_rec}, MediaRecorder refs={n_med}")

    hits = {}
    for name in so_files:
        data = z.read(name)
        found = []
        for marker, label in MARKERS:
            if marker in data:
                found.append(label)
        if found:
            hits[name] = found

    print(f"libs with audio-recording markers: {len(hits)}")
    for name, found in sorted(hits.items()):
        short = name.split("/")[-1]
        has_sles = any("OpenSL" in f for f in found)
        has_aa = any("AAudio" in f for f in found)
        has_java = any("JNI ref" in f for f in found)
        tag = []
        if has_sles: tag.append("SLES")
        if has_aa: tag.append("AAUDIO")
        if has_java: tag.append("JAVA")
        print(f"  {short:40s} [{'/'.join(tag):14s}] {'; '.join(found)}")

if __name__ == "__main__":
    for p in sys.argv[1:]:
        scan(p)
