#!/usr/bin/env python3
"""把本目录的图标重新内嵌进 ../index.html（favicon / apple-touch-icon / 页面 logo）。

什么时候需要重跑：
  · 换了图标——替换 underbox-logo-1024.png 后执行一次；
  · 想调尺寸，或改页面上那枚图标的大小；
  · index.html 里的 data URI 被人手改时误删了。

用法：python3 embed.py
依赖：macOS 自带的 sips（只用来缩放）。其它平台把 resized() 换成 ImageMagick 即可。

为什么内嵌而不是放成 .png 文件：页面不依赖任何外部图片请求，favicon 与 logo
第一次加载就有；部署也少一份要同步的文件。代价是 index.html 大约多 20KB——划算。
"""
import base64
import pathlib
import shutil
import subprocess
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SOURCE = HERE / "underbox-logo-1024.png"        # 一律从最大的那张缩，32px 放大会糊
HTML = HERE.parent / "index.html"
WORK = pathlib.Path(tempfile.mkdtemp(prefix="ub-icon-"))


def resized(size: int) -> pathlib.Path:
    """等比缩到 size×size。macOS 用 sips；没有就退回 PIL。"""
    out = WORK / f"logo-{size}.png"
    if shutil.which("sips"):
        subprocess.run(["sips", "-z", str(size), str(size), str(SOURCE), "--out", str(out)],
                       check=True, capture_output=True)
        return out
    from PIL import Image                            # noqa: PLC0415
    Image.open(SOURCE).convert("RGBA").resize((size, size), Image.LANCZOS).save(out, optimize=True)
    return out


def data_uri(size: int) -> str:
    return "data:image/png;base64," + base64.b64encode(resized(size).read_bytes()).decode()


def main() -> int:
    if not SOURCE.is_file():
        print(f"找不到源文件：{SOURCE}")
        return 1
    favicon, touch, mark = data_uri(64), data_uri(180), data_uri(96)
    html = HTML.read_text(encoding="utf-8")
    before = len(html)

    if 'rel="icon"' not in html:
        html = html.replace(
            "<title>UNDERBOX · TA 的优秀，滴水不漏</title>",
            "<title>UNDERBOX · TA 的优秀，滴水不漏</title>\n"
            f'<link rel="icon" type="image/png" sizes="64x64" href="{favicon}">\n'
            f'<link rel="apple-touch-icon" sizes="180x180" href="{touch}">\n'
            '<meta name="theme-color" content="#F6F3EC">', 1)
        print("① head：已内嵌 favicon + apple-touch-icon + theme-color")
    else:
        print("① head：已有，跳过")

    if "hero-logo" not in html:
        html = html.replace(
            '<h1 class="hero-mark">UNDER<em>BOX</em></h1>',
            f'<img class="hero-logo" src="{mark}" alt="" width="96" height="96">\n'
            '  <h1 class="hero-mark">UNDER<em>BOX</em></h1>', 1)
        print("② 登录页：品牌字上方已加图标")
    else:
        print("② 登录页：已有，跳过")

    if ".hero-logo{" not in html:
        html = html.replace(
            ".hero-mark{font-family:var(--disp)",
            ".hero-logo{display:block;width:clamp(76px,11vw,108px);height:auto;margin:0 auto 22px}\n"
            ".hero-mark{font-family:var(--disp)", 1)
        print("③ 样式：.hero-logo 已加入")
    else:
        print("③ 样式：已有，跳过")

    HTML.write_text(html, encoding="utf-8")
    print(f"\n体积：favicon {len(favicon)//1024}KB · 主屏图标 {len(touch)//1024}KB · 页面图标 {len(mark)//1024}KB")
    print(f"index.html：{before:,} → {len(html):,} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
