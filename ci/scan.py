#!/usr/bin/env python3
"""CI 第四阶段：扫描（scan）。

真实做的：
  1) 语法编译扫描   —— 把每个 .py 源码 compile 一遍，抓语法错误
  2) 静态规则扫描   —— 内置简化规则（等价于 semgrep/bandit 的自定义规则集），支持 # nosec 抑制
  3) 敏感信息扫描   —— 正则查私钥、AK/SK、硬编码口令
模拟做的（本机没有真实扫描器和漏洞库，按"没有的东西模拟一下"处理）：
  4) 镜像漏洞扫描   —— 模拟 trivy，对基础镜像给出 CVE 清单
  5) 依赖成分扫描   —— 模拟 pip-audit / SCA，本项目零第三方依赖

输出：ci/reports/scan-*.json，并按严重级别做质量门禁（gate）。
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = os.path.join(ROOT, "ci", "reports")

# 模拟开关：本地没有 trivy，用环境变量控制是否"扫出"高危漏洞，用来演示门禁拦截
SIM_CRITICAL = int(os.environ.get("SIM_CRITICAL", "0"))
SIM_HIGH = int(os.environ.get("SIM_HIGH", "1"))

# 静态规则（简化版 semgrep）：(规则ID, 正则, 等级, 说明)
RULES = [
    ("PY-EXEC",     r"\b(eval|exec)\s*\(",                     "HIGH",   "禁止使用 eval/exec 执行动态代码"),
    ("PY-SHELL",    r"\bos\.system\s*\(",                      "HIGH",   "禁止 os.system，存在命令注入风险"),
    ("PY-SHELL-T",  r"shell\s*=\s*True",                       "MEDIUM", "subprocess 使用 shell=True 有注入风险"),  # nosec
    ("PY-PICKLE",   r"\bpickle\.loads?\s*\(",                  "HIGH",   "反序列化不可信数据会导致 RCE"),
    ("PY-ASSERT",   r"^\s*assert\s",                           "LOW",    "生产代码不建议用 assert 做校验"),
    ("SEC-PRIVKEY", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",     "CRITICAL", "源码中不应出现私钥"),
    ("SEC-AK",      r"\b(AKID|AKIA)[0-9A-Z]{8,}",             "CRITICAL", "疑似云厂商 AK 硬编码"),
    ("SEC-PASSWD",  r"(?i)\b(password|passwd|secret)\s*=\s*[\"'][^\"']{6,}[\"']", "MEDIUM", "疑似硬编码口令"),
    ("SQL-FORMAT",  r"execute\(\s*[\"'].*%s",                  "MEDIUM", "SQL 应使用参数化查询，不要格式化拼接"),
]

SKIP_DIRS = {".git", "__pycache__", "logs", "uploads", "ci/reports", "ssl", ".venv"}
LEVEL_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}


def iter_py_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel = os.path.relpath(dirpath, ROOT).replace("\\", "/")
        if rel in SKIP_DIRS:
            continue
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def scan_compile():
    """真实：语法编译扫描（只编译不落盘 pyc）。"""
    findings = []
    for path in iter_py_files():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        try:
            with open(path, "rb") as fh:
                src = fh.read()
            compile(src, rel, "exec")
        except SyntaxError as exc:
            findings.append({"id": "PY-COMPILE", "level": "CRITICAL", "file": rel,
                             "line": getattr(exc, "lineno", 0) or 0,
                             "msg": "语法错误: %s" % exc.msg})
        except Exception as exc:
            findings.append({"id": "PY-COMPILE", "level": "CRITICAL", "file": rel,
                             "line": 0, "msg": "无法读取或编译: %s" % exc})
    return findings


def scan_rules():
    """真实：内置静态规则扫描（模拟 semgrep 的规则引擎行为），支持行尾 # nosec 抑制。"""
    findings = []
    for path in iter_py_files():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for no, line in enumerate(lines, 1):
            if "# nosec" in line:
                continue
            for rid, pattern, level, desc in RULES:
                if re.search(pattern, line):
                    findings.append({"id": rid, "level": level, "file": rel,
                                     "line": no, "msg": desc})
    return findings


def scan_image(image):
    """模拟：镜像漏洞扫描（本机无 trivy / 无漏洞库）。"""
    base = "python:3.12-slim"
    items = [
        {"id": "CVE-2024-0001-sim", "level": "CRITICAL", "pkg": "openssl-sim",
         "msg": "%s 基础镜像中的 openssl（模拟条目）" % base},
        {"id": "CVE-2024-0002-sim", "level": "HIGH", "pkg": "zlib-sim",
         "msg": "%s 基础镜像中的 zlib（模拟条目）" % base},
        {"id": "CVE-2024-0003-sim", "level": "MEDIUM", "pkg": "glibc-sim",
         "msg": "%s 基础镜像中的 glibc（模拟条目）" % base},
    ]
    pool = {"CRITICAL": SIM_CRITICAL, "HIGH": SIM_HIGH, "MEDIUM": 1}
    out = []
    for lv, cnt in pool.items():
        for it in items:
            if it["level"] == lv and len([x for x in out if x["level"] == lv]) < cnt:
                out.append(dict(it, file="image:%s" % image, line=0, simulated=True))
    return out


def scan_deps():
    """模拟：依赖成分扫描（SCA）。本项目纯标准库，无第三方依赖。"""
    req = os.path.join(ROOT, "requirements.txt")
    if not os.path.exists(req):
        return []
    return [{"id": "SCA-DECLARED", "level": "INFO", "file": "requirements.txt",
             "line": 0, "msg": "存在依赖清单，真实环境应接入 pip-audit / SCA 平台"}]


def gate(findings, max_critical, max_high):
    cnt = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0}
    for f in findings:
        cnt[f["level"]] = cnt.get(f["level"], 0) + 1
    reasons = []
    if cnt["CRITICAL"] > max_critical:
        reasons.append("CRITICAL %d > 上限 %d" % (cnt["CRITICAL"], max_critical))
    if cnt["HIGH"] > max_high:
        reasons.append("HIGH %d > 上限 %d" % (cnt["HIGH"], max_high))
    return cnt, reasons


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="screenshot-api:latest")
    ap.add_argument("--max-critical", type=int, default=0)
    ap.add_argument("--max-high", type=int, default=3)
    args = ap.parse_args()

    started = time.time()
    findings = []
    stages = []

    for name, fn in (("语法扫描", scan_compile),
                     ("静态规则扫描(模拟semgrep)", scan_rules),
                     ("镜像漏洞扫描(模拟trivy)", lambda: scan_image(args.image)),
                     ("依赖成分扫描(模拟SCA)", scan_deps)):
        t0 = time.time()
        got = fn()
        cost = int((time.time() - t0) * 1000)
        stages.append({"stage": name, "findings": len(got), "ms": cost})
        findings.extend(got)

    findings.sort(key=lambda f: (LEVEL_ORDER.get(f["level"], 9), f["file"], f["line"]))
    cnt, reasons = gate(findings, args.max_critical, args.max_high)

    report = {
        "image": args.image,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stages": stages,
        "counts": cnt,
        "gate": "FAILED" if reasons else "PASSED",
        "gate_reasons": reasons,
        "findings": findings,
        "note": "带 simulated=True 的条目为本地无扫描器时的模拟数据",
        "duration_ms": int((time.time() - started) * 1000),
    }

    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, "scan-%s.json" % time.strftime("%Y%m%d%H%M%S"))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print("=== scan 阶段明细 ===")
    for s in stages:
        print("  %-28s 发现 %d 条  %dms" % (s["stage"], s["findings"], s["ms"]))
    print("=== 问题清单 ===")
    if not findings:
        print("  无")
    for f in findings:
        tag = " [模拟]" if f.get("simulated") else ""
        print("  [%-8s] %-14s %s:%s  %s%s"
              % (f["level"], f["id"], f["file"], f["line"], f["msg"], tag))
    print("=== 门禁 ===")
    print("  统计: " + "  ".join("%s=%d" % (k, v) for k, v in cnt.items()))
    print("  结果: %s %s" % (report["gate"], ("-> " + "; ".join(reasons)) if reasons else ""))
    print("  报告: %s" % path)
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
