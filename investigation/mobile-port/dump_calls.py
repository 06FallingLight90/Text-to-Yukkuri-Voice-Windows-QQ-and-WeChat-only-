"""Dump outgoing calls of specific WeChat classes to identify their purpose."""
import sys, zipfile, io, re
from androguard.core.dex import DEX

INTEREST = re.compile(
    r"silk|amr|speex|aac|opus|voice|ptt|msg|send|upload| JNI|native|record|audio|"
    r"libwxaudio|mmmedia|media|encode|opus|file|db|storage", re.I)

TARGETS = ["Lyl/x;", "Lcm/e;", "Lcm/g;", "Lo51/l1;", "Lyl/w;", "Lyl/b0;", "Lvp0/n;", "Lzo/d;", "Lsz/f;", "Lsz/g;"]
DEXES = ["classes13.dex", "classes15.dex", "classes16.dex", "classes17.dex"]

def main(apk_path: str) -> None:
    z = zipfile.ZipFile(apk_path)
    for dn in DEXES:
        d = DEX(z.read(dn))
        found = []
        for cls in d.get_classes():
            name = cls.get_name()
            if name not in TARGETS:
                continue
            for m in cls.get_methods():
                code = m.get_code()
                if code is None:
                    continue
                calls = set()
                for ins in code.get_bc().get_instructions():
                    op = ins.get_name()
                    if op.startswith("invoke"):
                        try:
                            ref = ins.get_operands()[-1][2]
                            if isinstance(ref, str) and "->" in ref:
                                cls_name, meth = ref.split("->", 1)
                                meth = meth.split("(")[0]
                                calls.add(f"{cls_name}.{meth}")
                        except Exception:
                            pass
                interesting = sorted(c for c in calls if INTEREST.search(c) and not c.startswith("Ljava/") and not c.startswith("Lkotlin"))
                if interesting:
                    found.append(f"  {name}.{m.get_name()}:")
                    for c in interesting[:25]:
                        found.append(f"      {c}")
        if found:
            print(f"[{dn}]")
            print("\n".join(found))

if __name__ == "__main__":
    main(sys.argv[1])
