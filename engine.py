"""Local extraction and evidence retrieval; no network calls in this module."""
import io, math, re, unicodedata
from collections import Counter
import fitz
import pytesseract
from PIL import Image, ImageOps

MAX_PAGES = 32
Image.MAX_IMAGE_PIXELS = 25_000_000

def normalize(text):
    return unicodedata.normalize('NFC', text).replace('\x00', '').strip()

def tokens(text):
    # Keep Tamil combining marks attached to their letters.
    words = re.findall(r'[a-z0-9]+|[\u0b80-\u0bff]+', normalize(text).lower())
    stop = {'the','is','a','an','of','to','in','and','what','how','for','with','are'}
    return [w for w in words if w not in stop]

def ocr(image, lang):
    missing = set(lang.split('+')) - set(pytesseract.get_languages(config=''))
    if missing:
        raise ValueError('Missing Tesseract language data: ' + ', '.join(sorted(missing)))
    image = ImageOps.exif_transpose(image).convert('RGB')
    if image.width * image.height > 25_000_000:
        raise ValueError('Image exceeds 25 megapixels')
    data = pytesseract.image_to_data(image, lang=lang, config='--psm 3', output_type=pytesseract.Output.DICT, timeout=60)
    lines, scores = {}, []
    for i, word in enumerate(data['text']):
        if word.strip():
            key = (data['block_num'][i], data['par_num'][i], data['line_num'][i])
            lines.setdefault(key, []).append(word)
            if float(data['conf'][i]) >= 0: scores.append(float(data['conf'][i]))
    return '\n'.join(' '.join(v) for v in lines.values()), round(sum(scores)/len(scores), 1) if scores else 0

def extract(path, lang='tam+eng', force=False):
    raw = path.read_bytes()
    pages = []
    if raw.startswith(b'%PDF'):
        with fitz.open(stream=raw, filetype='pdf') as pdf:
            if pdf.needs_pass: raise ValueError('Password-protected PDFs are not supported; upload an unlocked copy')
            if not 1 <= len(pdf) <= MAX_PAGES: raise ValueError('PDF must contain 1–32 pages')
            for number, page in enumerate(pdf, 1):
                text = normalize(page.get_text(sort=True))
                method, confidence = 'embedded', None
                if force or len(text) < 30:
                    scale = min(2.0, math.sqrt(20_000_000/max(1, page.rect.width*page.rect.height)))
                    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                    text, confidence = ocr(Image.open(io.BytesIO(pix.tobytes('png'))), lang)
                    method = 'ocr'
                pages.append({'number':number, 'text':normalize(text), 'method':method, 'confidence':confidence})
    else:
        with Image.open(io.BytesIO(raw)) as im:
            if im.format not in {'PNG','JPEG','TIFF','WEBP'}: raise ValueError('Use PDF, PNG, JPEG, TIFF or WebP')
            count = getattr(im, 'n_frames', 1)
            if count > MAX_PAGES: raise ValueError('Image has more than 32 frames')
            for i in range(count):
                im.seek(i)
                text, confidence = ocr(im.copy(), lang)
                pages.append({'number':i+1,'text':normalize(text),'method':'ocr','confidence':confidence})
    if not any(p['text'] for p in pages): raise ValueError('No text found. Try a clearer scan or a different OCR language')
    if sum(len(p['text']) for p in pages) > 2_000_000: raise ValueError('Extracted text exceeds 2 million characters')
    return pages

def chunks(pages):
    result = []
    for page in pages:
        text = page['text']
        for start in range(0, len(text), 800):
            piece = text[start:start+1000]
            if piece.strip(): result.append({'page':page['number'], 'text':piece, 'start':start})
    return result

def retrieve(pages, query, limit=5):
    records = chunks(pages)
    terms = tokens(query)
    if not terms or not records: return []
    counts = [Counter(tokens(r['text'])) for r in records]
    avg = sum(sum(c.values()) for c in counts)/len(counts) or 1
    results = []
    for r, counts_i in zip(records, counts):
        score = 0
        for t in set(terms):
            df = sum(t in c for c in counts)
            f = counts_i[t]
            if f:
                idf = math.log(1+(len(records)-df+0.5)/(df+0.5))
                score += idf * f * 2.2/(f+1.2*(0.25+0.75*sum(counts_i.values())/avg))
        if score: results.append({**r,'score':round(score,4)})
    return sorted(results,key=lambda x:-x['score'])[:limit]

def summary(pages, limit=8):
    # Extractive summary: real source sentences only, in document order.
    candidates = []
    corpus = Counter(tokens(' '.join(p['text'] for p in pages)))
    for p in pages:
        for sentence in re.split(r'(?<=[.!?])\s+|\n+', p['text']):
            sentence = sentence.strip()
            if len(sentence) > 25:
                ts = tokens(sentence)
                score = sum(math.log1p(corpus[t]) for t in ts)/math.sqrt(len(ts) or 1)
                candidates.append({'page':p['number'],'text':sentence,'rank':score,'order':len(candidates)})
    chosen = sorted(candidates,key=lambda x:-x['rank'])[:limit]
    return [{'page':r['page'],'text':r['text']} for r in sorted(chosen,key=lambda x:x['order'])]
