import requests, pdfplumber, io
url='https://www.wimbledon.com/pdf/update/referees/2026/LS_Entries.pdf'
b=requests.get(url,timeout=30).content
print('bytes',len(b))
with pdfplumber.open(io.BytesIO(b)) as pdf:
    print('pages',len(pdf.pages))
    for i,p in enumerate(pdf.pages[:4], start=1):
        text=(p.extract_text() or '').splitlines()
        print('\n--- PAGE',i,'---')
        print('lines',len(text), 'width',p.width,'height',p.height)
        for ln in text[:25]:
            print(ln)
