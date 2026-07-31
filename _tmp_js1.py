# -*- coding: utf-8 -*-
"""임시: dashboard.html 인라인 JS 문법 검사 (실행 후 삭제)"""
import os
import re

import esprima

HERE = os.path.dirname(os.path.abspath(__file__))
path = os.path.join(HERE, "templates", "dashboard.html")
html = open(path, encoding="utf-8").read()
blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I)
lines = []
ok = True
for idx, block in enumerate(blocks):
    code = re.sub(r"\{\{.*?\}\}", "0", block, flags=re.S)
    code = re.sub(r"\{%.*?%\}", "", code, flags=re.S)
    # esprima(ES2017)는 ?. / ?? 를 모르므로 동등 표현으로 치환
    code = code.replace("?.[", "[").replace("?.(", "(").replace("?.", ".").replace("??", "||")
    try:
        esprima.parseScript(code, options={"tolerant": False})
        lines.append(f"[OK ] dashboard.html script #{idx+1}")
    except Exception as e:
        ok = False
        lines.append(f"[FAIL] dashboard.html script #{idx+1}: {e}")
        src = code.splitlines()
        m = re.search(r"Line (\d+)", str(e))
        if m:
            i = int(m.group(1)) - 1
            for j in range(max(0, i - 2), min(len(src), i + 3)):
                lines.append(("  >> " if j == i else "     ") + f"{j+1}: {src[j][:140]}")
lines.append("결과: " + ("정상" if ok else "문법 오류"))
with open(os.path.join(HERE, "_tmp_js1.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
