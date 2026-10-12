# Builds the single self-contained index.html: inlines the Google Fonts (latin + latin-ext only)
import re, base64, urllib.request, sys
src = open(__import__('os').path.join(__import__('os').path.dirname(__file__), 'sketch-lab.html')).read()
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'}
m = re.search(r'<link rel="stylesheet" href="(https://fonts\.googleapis\.com/[^"]+)">', src)
css = urllib.request.urlopen(urllib.request.Request(m.group(1).replace('&amp;', '&'), headers=UA)).read().decode()
blocks = re.findall(r'/\* ([\w-]+) \*/\s*(@font-face \{.*?\})', css, re.S)
out = []
for subset, block in blocks:
    if subset not in ('latin', 'latin-ext'): continue
    url = re.search(r'url\((https://[^)]+)\)', block).group(1)
    data = base64.b64encode(urllib.request.urlopen(urllib.request.Request(url, headers=UA)).read()).decode()
    out.append(block.replace(url, 'data:font/woff2;base64,' + data))
fontcss = '<style>\n/* IBM Plex Mono and Source Serif 4, SIL Open Font Licence, embedded so the page works offline */\n' + '\n'.join(out) + '\n</style>'
head = re.compile(r'<link rel="preconnect"[^>]*>\s*<link rel="preconnect"[^>]*>\s*<link rel="stylesheet" href="https://fonts\.googleapis\.com/[^"]+">')
assert head.search(src)
built = head.sub(lambda _: fontcss, src, count=1)
open(sys.argv[1] if len(sys.argv) > 1 else 'index.html', 'w').write(built)
print(len(out), 'font faces;', round(len(built) / 1024), 'KB')
