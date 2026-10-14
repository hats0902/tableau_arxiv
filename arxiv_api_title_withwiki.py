"""
arXiv APIで論文を月ごとに全件取得し、題名からキーワード（名詞句）を抽出するスクリプト

準備:
    pip install requests spacy
    python -m spacy download en_core_web_sm

出力（月ごとに追記。中断しても再実行すれば続きから再開）:
    papers.csv       : 論文ID, 題名, 要旨, 投稿日, 主カテゴリ, 全カテゴリ, URL
    keywords.csv     : 論文ID, 投稿年, キーワード, 論文内での出現回数（1論文に複数行）
    paper_categories.csv : 論文ID, カテゴリ, 主カテゴリか（1論文に複数行）
    done_months.txt  : 取得が完了した月の記録（再開用）
"""
import csv
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
 
import requests
import spacy
 
# ===== 設定 =====
CATEGORIES = [                    # 取得する分野（OR条件）
    "cs.CV", "eess.IV", "cs.GR", "cs.CL", "cs.IR", "cs.LG", "cs.AI",
    "stat.ML", "cs.MM", "cs.SD", "eess.AS", "cs.RO", "cs.HC", "stat.AP",
]
KEYWORDS = [                      # 検索語（OR条件）。題名か要旨に含む論文を取得。None にすると分野全体
    "sports", "sport", "athlete", "athletes", "athletic",
]
START_DATE = "2026-01-01"         # 取得期間の開始日（UTC, "YYYY-MM-DD"）
END_DATE = "2026-10-13"                   # 取得期間の終了日。None なら実行時点まで
MAX_RESULTS_PER_MONTH = None      # 1か月あたりの上限。None なら全件（テスト時は 5 など）
 
PAGE_SIZE = 200                   # 1リクエストあたりの件数
WAIT_SEC = 5                      # リクエスト間の待ち時間（arXivの利用ルール）
RETRY = 4                         # 429エラーや結果が空だったときの再試行回数
 
PAPERS_CSV = "papers.csv"
KEYWORDS_CSV = "keywords.csv"
CATEGORIES_CSV = "paper_categories.csv"
DONE_FILE = "done_months.txt"
WIKI_TERMS_TXT = "wiki_terms.txt"   # Wikipediaに記事があった複合語の一覧（リポジトリにコミットする）
 
ARXIV_API = "https://export.arxiv.org/api/query"
NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
 
PAPER_FIELDS = ["paper_id", "title", "abstract", "published", "category", "categories", "url"]
KEYWORD_FIELDS = ["paper_id", "year", "keyword", "count"]
CATEGORY_FIELDS = ["paper_id", "category", "is_primary"]
 
# 名詞句の先頭・末尾から削る品詞（冠詞、代名詞、数詞、前置詞など）
EDGE_POS = {"DET", "PRON", "NUM", "ADP", "CCONJ", "PUNCT", "PART", "SYM"}
 
# 名詞句の先頭・末尾に付きがちな、意味の薄い語
STOP_TOKENS = KEYWORDS
 
# 論文特有の汎用語（句全体がこれなら捨てる）
STOP_PHRASES = {
    "method", "approach", "paper", "result", "model", "framework", "task",
    "performance", "experiment", "dataset", "work", "study", "problem",
    "state-of-the-art", "accuracy", "baseline", "benchmark", "analysis",
    "technique", "system", "way", "challenge", "application", "information",
}
 
# ランダムに抽出した論文のキーワードを判定して作った除外語の一覧（1行1語。# で始まる行はコメント）
STOPWORDS_FILE = "stop_words.txt"
 
 
def load_stopword_file(path=STOPWORDS_FILE):
    """除外語の一覧ファイルを読み込む（小文字で比較する）。ファイルがなければ空"""
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {line.strip().lower() for line in f
                if line.strip() and not line.startswith("#")}
 
 
FILE_STOPWORDS = load_stopword_file()
 
 
def is_excluded(keyword):
    """最終的なキーワード（1語、または複合語）を除外するか。
    STOP_TOKENS（検索語など）、STOP_PHRASES（汎用語）、除外語の一覧ファイルのどれかに含まれていれば除外"""
    k = keyword.lower()
    # print(f'{k} in FILE_STOPWORDS: {k in FILE_STOPWORDS}')
    return k in STOP_TOKENS or k in STOP_PHRASES or k in FILE_STOPWORDS
 
 
# 表記ゆれの統合辞書（左を右にまとめる。長い表現から順に置換される）
SYNONYMS = {
    "large language model": "LLM",
    "vision-language model": "VLM",
    "vision language model": "VLM",
    "human pose estimation": "pose estimation",
    "convolutional neural network": "CNN",
    "multi object tracking": "multi-object tracking",
    "multiple object tracking": "multi-object tracking",
}
 
 
# ===== 期間・クエリ =====
def resolve_period(start_date, end_date):
    """設定値から取得期間(開始, 終了)をUTCのdatetimeで返す"""
    start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_date:
        end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end += timedelta(days=1) - timedelta(minutes=1)  # 終了日の23:59まで含める
    else:
        end = datetime.now(timezone.utc)
    if start > end:
        raise ValueError("START_DATE が END_DATE より後になっています")
    return start, end
 
 
def month_windows(start, end):
    """期間を1か月ごとに区切る"""
    cur = start
    while cur <= end:
        if cur.month == 12:
            nxt = cur.replace(year=cur.year + 1, month=1, day=1, hour=0, minute=0)
        else:
            nxt = cur.replace(month=cur.month + 1, day=1, hour=0, minute=0)
        yield cur, min(nxt - timedelta(minutes=1), end)
        cur = nxt
 
 
def build_query(categories, keywords, start, end):
    """分野・検索語・投稿日の範囲を組み合わせた検索クエリを作る"""
    cat_query = " OR ".join(f"cat:{c}" for c in categories)
    parts = [
        f"({cat_query})",
        f"submittedDate:[{start:%Y%m%d%H%M} TO {end:%Y%m%d%H%M}]",
    ]
    if keywords:
        # ti: 題名 / abs: 要旨。複数語の検索語は引用符で囲んで語句として検索する
        terms = [f'"{k}"' if " " in k else k for k in keywords]
        kw_query = " OR ".join(f"ti:{t} OR abs:{t}" for t in terms)
        parts.insert(0, f"({kw_query})")
    return " AND ".join(parts)
 
 
# ===== 取得 =====
def clean_text(text):
    """改行・連続空白をまとめ、LaTeXの数式($...$)を除去する"""
    text = re.sub(r"\$[^$]*\$", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()
 
 
def parse_entry(entry):
    url = entry.findtext("atom:id", namespaces=NS)
    primary = entry.find("arxiv:primary_category", NS)
    return {
        "paper_id": re.sub(r"v\d+$", "", url.rsplit("/abs/", 1)[-1]),  # v1, v2... を外す
        "title": clean_text(entry.findtext("atom:title", namespaces=NS)),
        "abstract": clean_text(entry.findtext("atom:summary", namespaces=NS)),
        "published": entry.findtext("atom:published", namespaces=NS)[:10],
        "category": primary.get("term") if primary is not None else "",
        "categories": ";".join(c.get("term") for c in entry.findall("atom:category", NS)),
        "url": url,
    }
 
 
def request_page(query, start):
    """1ページ分を取得する。結果が空なら時間をおいて再試行する"""
    params = {
        "search_query": query,
        "start": start,
        "max_results": PAGE_SIZE,
        "sortBy": "submittedDate",
        "sortOrder": "ascending",
    }
    total = 0
    for attempt in range(RETRY + 1):
        try:
            res = requests.get(ARXIV_API, params=params, timeout=120)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            # arXivの応答が遅い・接続が切れた場合も、待ってから再試行する
            if attempt >= RETRY:
                raise
            wait = 30 * (2 ** attempt)
            print(f"    {type(e).__name__}。{wait}秒後に再試行します（{attempt + 1}/{RETRY}）")
            time.sleep(wait)
            continue
        if res.status_code in (429, 500, 502, 503, 504) and attempt < RETRY:
            # 429: アクセスが多すぎる / 5xx: arXiv側の一時的な不調。待ってから再試行する
            retry_after = res.headers.get("Retry-After", "")
            wait = int(retry_after) if retry_after.isdigit() else 30 * (2 ** attempt)
            print(f"    HTTP {res.status_code}。{wait}秒後に再試行します（{attempt + 1}/{RETRY}）")
            time.sleep(wait)
            continue
        res.raise_for_status()
        root = ET.fromstring(res.text)
        total = int(root.findtext(f"{OPENSEARCH}totalResults") or 0)
        entries = root.findall("atom:entry", NS)
        if entries or total == 0 or start >= total:
            return total, entries
        print(f"    結果が空でした（start={start}）。{10 * (attempt + 1)}秒後に再試行します")
        time.sleep(10 * (attempt + 1))
    return total, []
 
 
def fetch_month(query, max_results):
    """1か月分を全件取得する。戻り値は (論文リスト, 取得予定件数)"""
    papers, start, limit = [], 0, None
    while True:
        total, entries = request_page(query, start)
        if limit is None:
            limit = total if max_results is None else min(total, max_results)
            print(f"  条件に合う件数: {total} / 取得予定: {limit}")
        if not entries or limit == 0:
            break
        papers.extend(parse_entry(e) for e in entries)
        start += len(entries)
        if start >= limit:
            break
        time.sleep(WAIT_SEC)
    return papers[:limit], limit
 
 
# ===== キーワード抽出 =====
# 固有表現抽出(NER)は使わないので無効にして高速化
nlp = spacy.load("en_core_web_sm", disable=["ner"])
 
# 名詞句を分割したときに、キーワードとして残す品詞（名詞・固有名詞）
KEEP_POS = {"NOUN", "PROPN"}
 
 
def normalize_token(tok):
    """略語(LLM, ViTなど)は大文字を保持、それ以外はレンマを小文字化"""
    if sum(c.isupper() for c in tok.text) >= 2:
        return re.sub(r"^([A-Z][A-Za-z]*[A-Z])s$", r"\1", tok.text)  # LLMs -> LLM
    return tok.lemma_.lower()
 
 
def is_edge_noise(tok):
    return (tok.pos_ in EDGE_POS or tok.is_stop
            or normalize_token(tok) in STOP_TOKENS)
 
 
def apply_synonyms(phrase):
    for src in sorted(SYNONYMS, key=len, reverse=True):
        phrase = re.sub(rf"\b{re.escape(src)}\b", SYNONYMS[src], phrase,
                        flags=re.IGNORECASE)
    return phrase
 
 
def lower_title_case(text):
    """頭文字だけが大文字の単語（Robots, Language など）を小文字にする。
    題名の大文字表記のせいで固有名詞と判定され、レンマ化（複数形→単数形）されないのを防ぐ。
    LLM, ViT, ResNet のように大文字が2文字以上ある略語はそのまま残す"""
    return re.sub(r"\b[A-Z][a-z]+\b", lambda m: m.group(0).lower(), text)
 
 
def is_word_token(tok):
    """記号・空白・数字ではなく、英字を含むトークンか"""
    return (not (tok.is_punct or tok.is_space or tok.like_num)
            and re.search(r"[A-Za-z]", tok.text) is not None)
 
 
def chunk_to_phrase(chunk):
    """名詞句の先頭・末尾の不要語を削り、(正規化した句, 残ったトークン) を返す。不要なら (None, [])"""
    toks = list(chunk)
    while toks and is_edge_noise(toks[0]):
        toks.pop(0)
    while toks and is_edge_noise(toks[-1]):
        toks.pop()
    if not toks:
        return None, []
    return span_phrase(toks), toks
 
 
def span_phrase(toks):
    """トークン列を正規化して、元の空白（ハイフンなど）を保ったまま連結する"""
    return "".join(normalize_token(t) + t.whitespace_ for t in toks).strip()
 
 
def is_candidate_span(toks):
    """Wikipediaと照合する部分句の条件：先頭と末尾が英字の語で、語が2つ以上"""
    return (is_word_token(toks[0]) and is_word_token(toks[-1])
            and sum(is_word_token(t) for t in toks) >= 2)
 
 
def candidate_phrases(toks):
    """名詞句の中の、連続する2語以上のすべての部分句（"machine learning approach" なら
    machine learning approach / machine learning / learning approach）"""
    phrases = set()
    for i in range(len(toks)):
        for j in range(i + 2, len(toks) + 1):
            span = toks[i:j]
            if is_candidate_span(span):
                phrases.add(span_phrase(span))
    return phrases
 
 
def segment_chunk(toks, cache):
    """名詞句を左から見ていき、Wikipediaに記事がある最も長い部分句を1語として取り出す。
    どの部分句にも当てはまらない語は、1語ずつ（名詞・固有名詞のみ）キーワードにする。
    例: machine learning approach → machine learning（記事あり）, approach（単語）"""
    # print(f'toks: {toks}')
    keywords = []
    i = 0
    while i < len(toks):
        matched = False
        for j in range(len(toks), i + 1, -1):  # 長い部分句から順に確認
            span = toks[i:j]
            if not is_candidate_span(span):
                continue
            phrase = span_phrase(span)
            # print(f'phrase: {phrase}')
            if cache.get(phrase, False):
                keyword = apply_synonyms(phrase)  # 表記ゆれもここで統合
                if not (is_excluded(phrase) or is_excluded(keyword)):
                    keywords.append(keyword)
                i = j
                matched = True
                break
        if not matched:
            keywords.extend(split_to_words([toks[i]]))
            i += 1
    return keywords
 
 
def split_to_words(toks):
    """Wikipediaにない名詞句を単語に分ける。名詞・固有名詞だけを残し、ストップワードを除く"""
    words = []
    for tok in toks:
        if tok.is_stop or not is_word_token(tok) or tok.pos_ not in KEEP_POS:
            continue
        word = normalize_token(tok)
        if is_excluded(word) or len(word) < 2:
            continue
        words.append(word)
    return words
 
 
# ----- Wikipedia の記事名との照合（MediaWiki API を使い、記事があった複合語だけをファイルに保存する） -----
WIKI_API = "https://en.wikipedia.org/w/api.php"
# Wikimediaの利用規約（User-Agent policy）で、連絡先を含むUser-Agentの設定が求められている
WIKI_HEADERS = {"User-Agent": "tableau-arxiv-dashboard/1.0 (https://github.com/hats0902/tableau_arxiv)"}
WIKI_BATCH = 50   # 1リクエストで問い合わせる記事名の数（APIの上限は50）
WIKI_WAIT = 1     # リクエスト間の待ち時間（秒）
 
 
def load_wiki_cache(path=WIKI_TERMS_TXT):
    """Wikipediaに記事があった複合語の一覧を読み込み、{複合語: True} の形で返す。
    記事がなかった句は保存していないので、次回の実行で改めて照合される"""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return {line.strip(): True for line in f if line.strip()}
 
 
def save_wiki_cache(cache, path=WIKI_TERMS_TXT):
    """記事があった複合語だけを保存する"""
    with open(path, "w", encoding="utf-8") as f:
        for phrase in sorted(p for p, found in cache.items() if found):
            f.write(phrase + "\n")
 
 
def query_wikipedia(titles):
    """記事名のリスト（最大50件）がWikipediaに存在するかを調べ、{記事名: True/False} を返す。
    リダイレクト（"Large language models" → "Large language model" など）も存在するものとして扱う"""
    params = {
        "action": "query", "format": "json", "formatversion": 2,
        "titles": "|".join(titles), "redirects": 1,
    }
    for attempt in range(RETRY + 1):
        try:
            res = requests.get(WIKI_API, params=params, headers=WIKI_HEADERS, timeout=30)
            res.raise_for_status()
            data = res.json()["query"]
            break
        except (requests.exceptions.RequestException, KeyError, ValueError) as e:
            if attempt >= RETRY:
                raise
            wait = 10 * (2 ** attempt)
            print(f"    Wikipedia照合で{type(e).__name__}。{wait}秒後に再試行します")
            time.sleep(wait)
    normalized = {n["from"]: n["to"] for n in data.get("normalized", [])}
    redirects = {r["from"]: r["to"] for r in data.get("redirects", [])}
    existing = {p["title"] for p in data.get("pages", [])
                if not p.get("missing") and not p.get("invalid")}
    result = {}
    for t in titles:
        title = normalized.get(t, t)       # 先頭の大文字化などの正規化
        title = redirects.get(title, title)  # リダイレクト先
        result[t] = title in existing
    return result
 
 
def check_wikipedia(phrases, cache):
    """キャッシュにない句だけをWikipediaに問い合わせ、cacheを更新する"""
    todo = sorted({p for p in phrases if p not in cache and "|" not in p})
    if not todo:
        return
    print(f"  Wikipediaで {len(todo)}件の名詞句を照合します")
    for i in range(0, len(todo), WIKI_BATCH):
        cache.update(query_wikipedia(todo[i:i + WIKI_BATCH]))
        time.sleep(WIKI_WAIT)
 
 
def extract_keyword_rows(papers):
    """複数の論文をまとめて処理し、keywords.csv用の行を返す。
    題名の名詞句の中で、Wikipediaに記事がある部分（最も長いもの）は1語のキーワードとして残し、
    それ以外の語は単語に分けて（名詞・固有名詞のみ）キーワードにする"""
    # 題名のみから抽出（要旨はpapers.csvに保存だけする）
    texts = [lower_title_case(p["title"]) for p in papers]
    docs = list(nlp.pipe(texts, batch_size=50))  # まとめて処理して高速化
 
    # 1. 名詞句を取り出す
    chunks_per_paper = []
    for doc in docs:
        items = []
        for chunk in doc.noun_chunks:
            phrase, toks = chunk_to_phrase(chunk)
            if phrase:
                items.append((phrase, toks))
        chunks_per_paper.append(items)
 
    # 2. 名詞句の中の2語以上の部分句をすべてWikipediaと照合（照合済みのものはキャッシュを使う）
    cache = load_wiki_cache()
    phrases = set()
    for items in chunks_per_paper:
        for _, toks in items:
            phrases |= candidate_phrases(toks)
    check_wikipedia(phrases, cache)
    save_wiki_cache(cache)
 
    # 3. キーワードにする
    rows = []
    for p, items in zip(papers, chunks_per_paper):
        counts = Counter()
        for _, toks in items:
            counts.update(segment_chunk(toks, cache))
        year = int(p["published"][:4])  # 投稿日(初版)の年
        rows.extend({"paper_id": p["paper_id"], "year": year, "keyword": kw, "count": c}
                    for kw, c in counts.most_common())
    # print(f'counts: {counts}')  
    return rows
 
 
# ===== カテゴリ（Tableau用の縦持ちデータ） =====
def category_rows(papers):
    """1論文に付いた全カテゴリ（arXivのオリジナル）を1行ずつに展開する
    取得対象外のカテゴリ（cs.NE など）も、論文に付いていればそのまま残す"""
    rows = []
    for p in papers:
        for cat in p["categories"].split(";"):
            if not cat:
                continue
            rows.append({
                "paper_id": p["paper_id"],
                "category": cat,
                "is_primary": cat == p["category"],
            })
    return rows
 
 
# ===== 保存・再開用の記録 =====
def load_existing_ids(path):
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {row["paper_id"] for row in csv.DictReader(f)}
 
 
def append_rows(path, rows, fieldnames):
    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)
 
 
def load_done_months():
    if not os.path.exists(DONE_FILE):
        return set()
    with open(DONE_FILE, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}
 
 
def mark_done(month_key):
    with open(DONE_FILE, "a", encoding="utf-8") as f:
        f.write(month_key + "\n")
 
 
# ===== 実行 =====
def main():
    start, end = resolve_period(START_DATE, END_DATE)
    print(f"取得期間: {start:%Y-%m-%d} 〜 {end:%Y-%m-%d} (UTC)\n")
 
    existing = load_existing_ids(PAPERS_CSV)
    done = load_done_months()
    now = datetime.now(timezone.utc)
 
    for w_start, w_end in month_windows(start, end):
        month_key = f"{w_start:%Y-%m}"
        if month_key in done:
            print(f"[{month_key}] 取得済みのためスキップ")
            continue
 
        print(f"[{month_key}] {w_start:%Y-%m-%d} 〜 {w_end:%Y-%m-%d}")
        query = build_query(CATEGORIES, KEYWORDS, w_start, w_end)
        papers, limit = fetch_month(query, MAX_RESULTS_PER_MONTH)
 
        new_papers = [p for p in papers if p["paper_id"] not in existing]
        if new_papers:
            append_rows(PAPERS_CSV, new_papers, PAPER_FIELDS)
            append_rows(KEYWORDS_CSV, extract_keyword_rows(new_papers), KEYWORD_FIELDS)
            append_rows(CATEGORIES_CSV, category_rows(new_papers), CATEGORY_FIELDS)
            existing.update(p["paper_id"] for p in new_papers)
        print(f"  取得 {len(papers)}件 / 新規保存 {len(new_papers)}件")
 
        # 予定件数をすべて取れていて、かつ月が終わっている場合だけ「完了」と記録する
        # （進行中の月は、次回の定期実行で取り直して新しい論文を追加する）
        month_finished = w_end < now - timedelta(days=1)
        if len(papers) >= limit and month_finished and MAX_RESULTS_PER_MONTH is None:
            mark_done(month_key)
        elif len(papers) < limit:
            print(f"  {limit - len(papers)}件取りこぼしました。再実行すると取り直します")
 
        time.sleep(WAIT_SEC)
 
    print("\n完了しました")


if __name__ == "__main__":
    main()
    
    # papers = [
    #     {'title': 'EventAnchor: Reducing Human Interactions in Event Annotation of Racket Sports Videos', 'published': "00000000", "paper_id": 0.000},
    #     # {'title': "Reinforcement Learning for Robust Athletic Intelligence: Lessons from the 2nd 'AI Olympics with RealAIGym' Competition", 'published': "00000000"}
    # ]
    # extract_keyword_rows(papers)