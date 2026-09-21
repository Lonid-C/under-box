#!/usr/bin/env python3
"""生成 sample-resume.pdf —— 给 PDF 查看器用的三页样本简历。

内容取自 `_data.json`（也就是 open_box 那份虚构 fixture），按类别分三页铺开：
  第 1 页  基本信息 · 教育经历 · 校内荣誉 · 奖学金
  第 2 页  学生工作 · 竞赛
  第 3 页  论文 · 开源项目 · 实习
这样 PDF 里读到的和右栏台账里核的是同一份材料，演示时能对着看。

用 reportlab（`pip install reportlab`）。虚构数据，域名全部 .example，不指向真实站点。
"""
from __future__ import annotations

import json
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent
OUT = HERE / "sample-resume.pdf"
FONT = "STSong-Light"

PAGES = [
    ("基本信息 · 教育经历 · 校内荣誉 · 奖学金", {"学历", "校内荣誉", "奖学金"}),
    ("学生工作 · 竞赛", {"学生工作", "竞赛"}),
    ("论文 · 开源项目 · 实习", {"论文", "开源项目", "实习"}),
]


def wrap(draw, text: str, width: float, size: int) -> list[str]:
    """按实际宽度折行——中文没有空格可断，只能逐字量。"""
    lines, cur = [], ""
    for ch in text:
        if draw.stringWidth(cur + ch, FONT, size) > width:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def build() -> Path:
    report = json.loads((HERE / "_data.json").read_text(encoding="utf-8"))
    pdfmetrics.registerFont(UnicodeCIDFont(FONT))

    W, H = A4
    c = canvas.Canvas(str(OUT), pagesize=A4)
    c.setTitle("履历核验样本（虚构）")
    c.setAuthor(report["candidate"])
    margin, usable = 22 * mm, W - 44 * mm

    for idx, (title, cats) in enumerate(PAGES):
        y = H - margin

        c.setFont(FONT, 9)
        c.setFillGray(0.45)
        c.drawString(margin, y, "履历核验样本 · 全部内容为虚构，域名使用 RFC 6761/2606 保留的 .example")
        y -= 14 * mm

        c.setFillGray(0.1)
        c.setFont(FONT, 20)
        c.drawString(margin, y, report["candidate"])
        c.setFont(FONT, 10)
        c.setFillGray(0.4)
        c.drawString(margin + c.stringWidth(report["candidate"], FONT, 20) + 6 * mm, y + 1 * mm,
                     f"应聘 {report['position']}")
        y -= 9 * mm

        c.setStrokeGray(0.15)
        c.setLineWidth(1.4)
        c.line(margin, y, W - margin, y)
        y -= 11 * mm

        c.setFillGray(0.1)
        c.setFont(FONT, 13)
        c.drawString(margin, y, title)
        y -= 9 * mm

        for claim in report["claims"]:
            if claim["category"] not in cats:
                continue
            c.setFont(FONT, 9)
            c.setFillGray(0.45)
            c.drawString(margin, y, claim.get("date") or "时间未标注")
            c.setFillGray(0.12)
            c.setFont(FONT, 11)
            for line in wrap(c, claim["rawText"], usable - 26 * mm, 11):
                c.drawString(margin + 24 * mm, y, line)
                y -= 6.4 * mm
            c.setFillGray(0.42)
            c.setFont(FONT, 9)
            for line in wrap(c, "　".join(claim.get("elements") or []), usable - 24 * mm, 9):
                c.drawString(margin + 24 * mm, y, line)
                y -= 5.4 * mm
            y -= 3.4 * mm

        c.setFont(FONT, 9)
        c.setFillGray(0.55)
        c.drawRightString(W - margin, margin - 6 * mm, f"第 {idx + 1} 页 / 共 {len(PAGES)} 页")
        c.showPage()

    c.save()
    return OUT


if __name__ == "__main__":
    p = build()
    print(f"已生成 {p}　{p.stat().st_size / 1024:.1f} KB　{len(PAGES)} 页")
