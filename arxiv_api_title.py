"""
arXiv APIで論文を月ごとに全件取得し、題名からキーワード（名詞句）を抽出するスクリプト

準備:
    pip install requests spacy
    python -m spacy download en_core_web_sm

出力（月ごとに追記。中断しても再実行すれば続きから再開）:
    papers.csv       : 論文ID, 題名, 要旨, 投稿日, 主カテゴリ, 全カテゴリ, URL
    keywords.csv     : 論文ID, キーワード, 論文内での出現回数（1論文に複数行）
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
KEYWORDS = [                      # 検索語（OR条件）。None にすると分野全体
    "sports", "sport", "soccer", "football", "basketball", "volleyball",
    "tennis", "badminton", "baseball", "golf", "athlete",
]
START_DATE = "2024-01-01"         # 取得期間の開始日（UTC, "YYYY-MM-DD"）
END_DATE = None                   # 取得期間の終了日。None なら実行時点まで
MAX_RESULTS_PER_MONTH = None      # 1か月あたりの上限。None なら全件（テスト時は 5 など）

PAGE_SIZE = 200                   # 1リクエストあたりの件数
WAIT_SEC = 3                      # リクエスト間の待ち時間（arXivの利用ルール）
RETRY = 3                         # 結果が空だったときの再試行回数

PAPERS_CSV = "papers.csv"
KEYWORDS_CSV = "keywords.csv"
CATEGORIES_CSV = "paper_categories.csv"
DONE_FILE = "done_months.txt"

ARXIV_API = "https://export.arxiv.org/api/query"
NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"

PAPER_FIELDS = ["paper_id", "title", "abstract", "published", "category", "categories", "url"]
KEYWORD_FIELDS = ["paper_id", "year", "keyword", "count"]
CATEGORY_FIELDS = ["paper_id", "category", "is_primary"]

# 名詞句の先頭・末尾から削る品詞（冠詞、代名詞、数詞、前置詞など）
EDGE_POS = {"DET", "PRON", "NUM", "ADP", "CCONJ", "PUNCT", "PART", "SYM"}

# 名詞句の先頭・末尾に付きがちな、意味の薄い語
STOP_TOKENS = {
    "novel", "new", "proposed", "various", "different", "existing", "recent",
    "several", "many", "such", "other", "same", "previous", "current", "key",
    "purpose", "idea", "time", "activity", "rule", "subject", "sports", "sport"
}

# 論文特有の汎用語（句全体がこれなら捨てる）
STOP_PHRASES = {
    "method", "approach", "paper", "result", "model", "framework", "task",
    "performance", "experiment", "dataset", "work", "study", "problem",
    "state-of-the-art", "accuracy", "baseline", "benchmark", "analysis",
    "technique", "system", "way", "challenge", "application", "information",
}

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
        kw_query = " OR ".join(f"all:{k}" for k in keywords)
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
    for attempt in range(RETRY + 1):
        res = requests.get(ARXIV_API, params=params, timeout=60)
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


def normalize_token(tok):
    """
    略語(LLM, ViTなど)は大文字を保持、それ以外はレンマを小文字化
    大文字が2つ以上あれば略語と判断している
    """
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


def chunk_to_keyword(chunk):
    """名詞句1つを正規化されたキーワードに変換する。不要ならNoneを返す"""
    toks = list(chunk)
    while toks and is_edge_noise(toks[0]):
        toks.pop(0)
    while toks and is_edge_noise(toks[-1]):
        toks.pop()
    if not toks:
        return None

    # トークンごとに正規化し、元の空白（ハイフンなど）を保ったまま連結
    phrase = "".join(normalize_token(t) + t.whitespace_ for t in toks).strip()
    phrase = apply_synonyms(phrase)

    if phrase.lower() in STOP_PHRASES:
        return None
    if len(phrase) < 3 or not re.search(r"[A-Za-z]", phrase):
        return None
    return phrase


def extract_keyword_rows(papers):
    """複数の論文をまとめて処理し、keywords.csv用の行を返す"""
    texts = (p["title"] for p in papers)  # 題名のみから抽出（要旨はpapers.csvに保存だけする）
    rows = []
    for p, doc in zip(papers, nlp.pipe(texts, batch_size=50)):  # まとめて処理して高速化
        counts = Counter()
        # print(f'doc: {doc}')
        for chunk in doc.noun_chunks: # 名詞だけ取り出す
            kw = chunk_to_keyword(chunk)
            if kw:
                counts[kw] += 1
        year = int(p["published"][:4])  # 投稿日(初版)の年
        rows.extend({"paper_id": p["paper_id"], "year": year, "keyword": kw, "count": c}
            for kw, c in counts.most_common())
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