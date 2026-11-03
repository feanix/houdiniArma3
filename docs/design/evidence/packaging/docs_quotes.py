"""Print the SideFX documentation lines the spike document cites.

Online: sidefx.com product and licensing pages (fetched now).
Local: the Houdini 22.0.466 help zips in the Indie install.

Run: python docs_quotes.py
"""
import html
import io
import re
import sys
import urllib.request
import zipfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HELP = "E:/SteamLibrary/steamapps/common/Houdini Indie/houdini/help/%s.zip"


def web_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    t = urllib.request.urlopen(req, timeout=30).read().decode("utf8", "replace")
    t = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", t)
    t = re.sub(r"<[^>]+>", "\n", t)
    return re.sub(r"\n\s*\n+", "\n", html.unescape(t))


def show_web(url, patterns):
    print("\n## %s" % url)
    lines = web_text(url).splitlines()
    for i, line in enumerate(lines):
        if any(re.search(p, line, re.I) for p in patterns):
            print("  %s" % " ".join(x.strip() for x in lines[i:i + 1]))


def show_zip(zname, member, patterns, context=1):
    print("\n## local help: %s.zip :: %s" % (zname, member))
    lines = zipfile.ZipFile(HELP % zname).read(member).decode("utf8", "replace").splitlines()
    for i, line in enumerate(lines):
        if any(re.search(p, line, re.I) for p in patterns):
            for x in lines[max(0, i - context):i + context + 1]:
                print("  " + x)
            print("  --")


show_web("https://www.sidefx.com/Support/licensing/",
         [r"indie \[limited", r"hdalc", r"hdanc", r"\.hip/\.hda", r"File Formats"])
show_web("https://www.sidefx.com/products/compare/",
         [r"different file formats", r"^\.hdalc|^\.hdanc|^\.hiplc|^\.hipnc"])
show_web("https://www.sidefx.com/products/houdini-indie/",
         [r"own file format", r"same pipeline"])
show_web("https://www.sidefx.com/products/houdini-apprentice/",
         [r"virtually all of the features", r"own file format", r"same pipeline",
          r"Digital Assets created in Apprentice", r"Non-commercial projects"])
show_web("https://www.sidefx.com/Support/licensing/indie/",
         [r"HDALC"])

show_web("https://www.sidefx.com/docs/houdini/ref/utils/hotl.html",
         [r"install assets from an expanded directory", r"Contents\.mime", r"version control",
          r"three expansion options"])

show_zip("hom", "hou/hda.txt", [r"defaultFileExtension"], 2)
show_zip("hom", "hou/saveMode.txt", [r"commercial license"], 3)
show_zip("hom", "pythonsop.txt", [r"Do not set node parameters", r"creating a new node type",
                                  r"Set the __Operator Definition__"], 2)
show_zip("nodes", "sop/python.txt", [r"reusable geometry node type", r"Maintain State"], 1)
show_zip("hom", "hou/Parm.txt", [r"pressButton"], 4)
