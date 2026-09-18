import os
def load(path=".env"):
    for line in open(path, encoding="utf-8-sig").read().replace("\r","").split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
