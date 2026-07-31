# -*- coding: utf-8 -*-
"""임시: 수정한 JS 전체 문법 검사 (실행 후 삭제)"""
import glob
import os
import re

import esprima

HERE = os.path.dirname(os.path.abspath(__file__))
out = []
fails = []


def parse(name, code):
    # esprima(ES2017)는 ?. / ?? 를 모르므로 동등 표현으로 치환 후 검사
    code = code.replace("?.[", "[").replace("?.(", "(").replace("?.", ".").replace("??=", "=").replace("??", "||")
    try:
        esprima.parseScript(code, options={"tolerant": False})
        return True
    except Exception as e:
        out.append(f"[FAIL] {name}: {e}")
        src = code.splitlines()
        m = re.search(r"Line (\d+)", str(e))
        if m:
            i = int(m.group(1)) - 1
            for j in range(max(0, i - 2), min(len(src), i + 3)):
                out.append(("  >> " if j == i else "     ") + f"{j+1}: {src[j][:140]}")
        fails.append(name)
        return False


for path in [os.path.join(HERE, "static", "common.js"), os.path.join(HERE, "static", "sw.js")]:
    if parse(os.path.basename(path), open(path, encoding="utf-8").read()):
        out.append(f"[OK ] {os.path.basename(path)}")

for path in sorted(glob.glob(os.path.join(HERE, "templates", "*.html"))):
    name = os.path.basename(path)
    html = open(path, encoding="utf-8").read()
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I)
    ok = True
    for idx, block in enumerate(blocks):
        code = re.sub(r"\{\{.*?\}\}", "0", block, flags=re.S)
        code = re.sub(r"\{%.*?%\}", "", code, flags=re.S)
        if not parse(f"{name} #{idx+1}", code):
            ok = False
    if ok:
        out.append(f"[OK ] {name} (스크립트 {len(blocks)}개)")

out.append("결과: " + ("JS 문법 전부 정상" if not fails else f"문법 오류 {fails}"))
with open(os.path.join(HERE, "_tmp_js2.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(out))
