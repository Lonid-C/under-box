"""按印刷目录的物理栏目读书目，防止双页目录把别本书的责任者拼过来。"""
import re
import unicodedata

def pdf_catalog_text(data):
    try:
        import pymupdf
        result=[]
        with pymupdf.open(stream=data,filetype='pdf') as doc:
            for i,page in enumerate(doc):
                whole=unicodedata.normalize('NFKC',page.get_text())
                w,h=page.rect.width,page.rect.height
                left=unicodedata.normalize('NFKC',page.get_text(clip=pymupdf.Rect(0,0,w/2,h),sort=True))
                right=unicodedata.normalize('NFKC',page.get_text(clip=pymupdf.Rect(w/2,0,w,h),sort=True))
                catalog=lambda t: bool(re.search(r'作\s*者\s*[/／]\s*编\s*者',t) and re.search(r'I\s*S\s*B\s*N',t,re.I))
                parts=[left,right] if catalog(left) and catalog(right) else [whole]
                for n,t in enumerate(parts):
                    result.append(f'［书目条目 · 第{i+1}页 · 栏{n+1}］\n'+t)
        return '\n'.join(result).strip() or None
    except Exception:
        return None
