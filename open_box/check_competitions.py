"""检查赛事预设的公开入口并导出研究记录；不调用搜索付费 API 或模型。"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.competitions import allowed_official_url, load_catalog
from app.parse import extract_main_text
from app.search import PageFetcher

RESULTS = re.compile(r"获奖|授奖|赛果|成绩公示|成绩公布|获奖查询|\b(?:results?|winners?|awards?|standings|scoreboard)\b", re.I)


def probe(item: dict, timeout: float = 8) -> dict:
    fetcher = PageFetcher(timeout=timeout)
    out = dict(id=item["id"],name=item["name"],requests=[],award_links=[])
    urls = [*item.get("official_urls", [])[:1], *(p["url"] for p in item.get("result_pages", [])[:1])]
    pending = list(dict.fromkeys(urls))
    seen = set()
    while pending and len(seen) < 3:
        url = pending.pop(0)
        if url in seen:
            continue
        seen.add(url)
        started = time.monotonic()
        try:
            content = fetcher.get(url)
        except Exception as exc:
            content = None
            fetcher.failures[url] = type(exc).__name__
        result = dict(url=url,seconds=round(time.monotonic()-started,2),
                      readable=bool(content),failure=fetcher.failures.get(url,""))
        resolved = fetcher.resolved_urls.get(url,url)
        result['resolved_url'] = resolved
        if content:
            text = extract_main_text(content) if "<" in content[:2000] else content
            soup = BeautifulSoup(content,"lxml") if "<" in content[:2000] else None
            result["title"] = soup.title.get_text(" ",strip=True) if soup and soup.title else ""
            result["result_text"] = bool(RESULTS.search(text))
            # 仅供研究人工复核，不能把出现“获奖”几个字自动认成公开名单。
            result["excerpt"] = text[:1600]
            if soup:
                for a in soup.select("a[href]"):
                    label=a.get("title") or a.get_text(" ",strip=True)
                    target=urljoin(resolved,a.get("href",""))
                    if RESULTS.search(label) and allowed_official_url(item,target):
                        link=dict(url=target,title=label,from_url=resolved)
                        if link not in out["award_links"]:
                            out["award_links"].append(link)
                if len(seen)<2:
                    candidates=sorted(out["award_links"],key=lambda x:(not bool(re.search(r"202[456]",x["title"])),len(x["title"])))
                    pending.extend(x["url"] for x in candidates[:2] if x["url"] not in seen)
        out["requests"].append(result)
    return out


def render_catalog(doc: dict, path: Path) -> None:
    labels={"public_names":"有姓名名单","public_teams":"有团队/学校赛果","public_results":"有公告/查询入口",
            "login_required":"需要登录查询","not_public":"官方明确不公开","not_found":"暂未找到公开入口","unknown":"待确认"}
    records=doc["competitions"]
    counts=Counter(r["publication"]["status"] for r in records)
    confirmed=sum(counts[s] for s in ("public_names", "public_teams", "public_results"))
    lines=["# 大学阶段竞赛官网与获奖名单核验目录","",f"研究日期：{doc['checked_at']}。共 {len(records)} 项赛事预设。","",
           "本目录以教育部赛事通知、公开存档的学会竞赛目录和主办方官网为依据。学会目录不等于教育部主办名单；国际扩展也不表示教育部认定或奖项含金量排序。","",
           f"已确认公开名单、团队赛果、获奖公告或查询入口的赛事共 {confirmed} 项；尚待确认公开入口 {counts['unknown']} 项。样例只证明所注明年份/组别的公开范围，不保证所有年度和赛道都能查到本人。","",
           "## 检索流程","","简历原文 → 识别赛事全称/别名 → 按对应年份、届次、赛道与阶段选择官网 → 先读取官方名单或历届索引 → 跟进 HTML/PDF/DOCX/XLSX 附件 → 核对本人姓名、学校、队号/队名、作品及奖级 → 必要时补查学校官网。","",
           "名单可用性和本人是否获奖分开记录。目录本身不是证据；团队赛果不单独证明个人成员身份；名单没有某人不等于奖项不实。只有官方明确说明不公开名单时，报告才写“官网不提供公开获奖名单”；网页打不开、robots 禁止、登录门槛或搜索不到分别记录。","",
           "公开范围会随年份、赛道及维护情况变化。‘有名单’指至少确认过一项公开官方样例，不能据此断言每年每组都有；‘待确认’保留学校官网等补充检索，不自动跳过核验。","",
           "## 来源","",*[f"- [{b['description']}]({b['url']})" for b in doc["basis"]],""]
    subjects=list(dict.fromkeys(s for r in records for s in r["subjects"]))
    for subject in subjects:
        lines += [f"## {subject}","","| 赛事 | 地区 | 官网/主办方入口 | 名单可用性 | 已确认入口与范围 |","|---|---|---|---|---|"]
        for r in records:
            if subject not in r["subjects"]:
                continue
            urls="<br>".join(f"[官网{i+1}]({u})" for i,u in enumerate(r.get("official_urls",[]))) or "暂无固定网站预设；保留动态发现/学校官网"
            pages="<br>".join(f"[{'历届/查询入口' if p['kind']=='index' else str(p.get('year') or '')+'名单样例'}{'（本次失效，已暂停直读）' if p.get('inactive') else ''}]({p['url']})" for p in r.get("result_pages",[]))
            note=r["publication"].get("scope_note","").replace("|","/").replace("\n"," ")
            scope="<br>".join(v for v in (pages,note) if v)
            lines.append(f"| {r['name']} | {'国际' if r['region']=='International' else '国内目录'} | {urls} | {labels[r['publication']['status']]} | {scope} |")
        lines += [""]
    lines += ["## 实测路径与读取限制","",
              "- 美赛：COMAP 历届索引 → 2024 Results → MCM A/B/C 三份完整赛果 PDF，已成功提取文本。网页会跳转并补末尾斜杠，附件必须使用跳转后的地址解析；按 MCM/ICM 类型及题目结果排序，普通获奖核验不优先读取奖学金新闻。名单主要是队号和学校，仍需成员关联证明。",
              "- 职业规划大赛：教育部 2025 正式结果通知 → 成长赛道/就业赛道 DOCX，已成功提取名单文本；学生和教师赛道分开选择。",
              "- 外教社跨文化大赛：主办/承办方 2024 赛事报道包含全国决赛与总决赛获奖名单，但名单为图片；当前文本抽取不会凭图片标题核实姓名，需人工读图或学校官方佐证。",
              "- 水利创新设计大赛：主办分会公开结果附件；2025 PDF 本次未提取到可用文本，2023 PDF 有可读姓名名单但属于推荐获奖公示。不能把扫描件无法读取写成官网没有名单。",
              "- 教育部大学生在线数学建模获奖查询入口依赖动态加载；静态页面未读出个人结果时继续查询赛事官网年度公告，不据此下获奖结论。",
              "- 本轮只测试公开网页与读取路线，没有调用 GLM 或 DeepSeek 付费接口，也未以实际候选人身份进行最终核验。离线模型桩只用于验证预算、分级与防止误判的规则。","",
              "## 维护与复核","","机器可维护配置为 `data/competitions.json`，包含别名、学科、官网域名、官方名单样例、适用年份、公开范围和来源。新增赛事需给出官网依据；标记不公开需提供官方政策链接及原文说明。","",
              "运行 `python check_competitions.py --output out/research/competition-probe.json` 可检查公开页面及名单链接。脚本只读取公开页面、遵守 robots，最多每项三页；网络失败不会自动改写公开名单状态。`--render-only` 只更新本 Markdown，不发网络请求。","",
              "国家奖学金、校内优秀学生、优秀毕业生等不是统一赛事，继续走对应学校或主管部门公示流程。省级、校级获奖和全国总决赛获奖必须分别核对。",""]
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text("\n".join(lines),encoding="utf-8")


def main() -> None:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path,default=Path("out/research/competition-probe.json"))
    p.add_argument("--workers",type=int,default=4)
    p.add_argument("--timeout",type=float,default=8)
    p.add_argument("--limit",type=int,default=0)
    p.add_argument("--render-only",action="store_true")
    args=p.parse_args()
    doc=load_catalog()
    if not args.render_only:
        items=doc["competitions"][:args.limit or None]
        results=[]
        with ThreadPoolExecutor(max_workers=max(1,min(args.workers,6))) as pool:
            for result in pool.map(lambda item:probe(item,args.timeout),items):
                results.append(result)
                args.output.parent.mkdir(parents=True,exist_ok=True)
                args.output.write_text(json.dumps(dict(checked_at=datetime.now(timezone.utc).isoformat(),probes=results),ensure_ascii=False,indent=2)+"\n")
                print(f"{result['id']}: {sum(p['readable'] for p in result['requests'])}/{len(result['requests'])}页可读；{len(result['award_links'])}个名单候选链接",flush=True)
    render_catalog(doc,Path(__file__).resolve().parent/"docs/COMPETITION_CATALOG.md")


if __name__=="__main__":
    main()
