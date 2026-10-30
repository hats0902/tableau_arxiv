"""
Gemini API で論文の要旨から「競技・タスク・手法・モデル・モダリティ」を抽出するテスト用スクリプト。
papers.csv から数件を選んで抽出し、結果を画面に表示して llm_test_output.json に保存する。
スプレッドシートや keywords.csv は変更しない。

準備:
    1. Google AI Studio (https://aistudio.google.com/) で APIキーを発行する
    2. 環境変数に設定する
           export GEMINI_API_KEY=xxxxxxxx
    3. 使えるモデル名を確認し、下の MODEL に書く
           python llm_extract_test.py --list-models
    4. 論文の読み込み元を PAPERS_PATH に書く
           papers.csv          : リポジトリの CSV（git pull で最新にしておく）
           keywords.csv.gsheet : Google ドライブのショートカット。中の doc_id からスプレッドシートを特定し、
                                 papers シートを読む（GCP_SA_KEY_FILE が必要。
                                 upload_to_sheets.py と同じフォルダで実行する）

使い方:
    python llm_extract_test.py                          # PAPER_IDS の論文（空ならランダムに N_PAPERS 件）
    python llm_extract_test.py 2101.00400 2105.01234    # コマンドで論文IDを指定（PAPER_IDS より優先）

出力:
    各項目に当てはまる語がなければ null にする（例: "sports": null）。
    指定した論文IDが papers に見つからない場合は、その論文の "result" を null にする。
"""
import csv
import json
import os
import random
import re
import sys
import time

import requests

# ===== 設定 =====
MODEL = "gemini-flash-lite-latest"  # --list-models で確認したモデル名に置き換える（本番ではバージョン付きの名前に固定する）
# 論文の読み込み元。.csv なら CSV を、.gsheet ならそのスプレッドシートの papers シートを読む
# （.gsheet はどのシート名のファイルでもよい。中の doc_id からスプレッドシートを特定する）
PAPERS_PATH = "papers.csv"
PAPERS_SHEET = "papers"  # .gsheet のときに読むシート名（要旨の列があるシート）
OUTPUT_JSON = "llm_test_output.json"
PAPER_IDS = []        # テストする論文ID（例: ["2101.00400", "2105.01234"]）。空ならランダムに選ぶ
N_PAPERS = 5          # PAPER_IDS が空のときに、ランダムに選ぶ論文の数
SEED = 0              # ランダムに選ぶときのシード（同じ論文で試し直せるように固定）
WAIT_SEC = 5          # リクエストの間隔（無料枠の1分あたりの回数制限に配慮）
RETRY = 4

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
FIELDS = ["sports", "task", "methods", "model", "modality"]

# 出力の形。各項目は {"term": 集計用の語, "source": 根拠となる本文の表記} のリスト
ITEM_SCHEMA = {
    "type": "OBJECT",
    "properties": {"term": {"type": "STRING"}, "source": {"type": "STRING"}},
    "required": ["term", "source"],
    "propertyOrdering": ["term", "source"],
}
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {f: {"type": "ARRAY", "items": ITEM_SCHEMA} for f in FIELDS},
    "required": FIELDS,
    "propertyOrdering": FIELDS,
}

PROMPT = """You extract keywords from an arXiv paper about sports and AI/ML.
Read the title and abstract, and return five lists.
Each list item has two fields:
- term: a short keyword used to count papers (may be normalized, see the rules)
- source: the exact words in the title or abstract that support the term,
  copied character for character (a contiguous span, not a paraphrase)

- sports: the sports, events, competitions, or leagues the paper itself studies
  (e.g. "soccer", "table tennis", "NBA", "FIFA World Cup").
  Do not output generic words such as "sport", "sports", "athlete", "game", "match".
- task: the problems the paper itself addresses — what is being predicted, recognized, or generated
  (e.g. "pose estimation", "action recognition", "player tracking").
- methods: learning paradigms and technique families the paper itself uses
  (e.g. "deep learning", "machine learning", "reinforcement learning", "self-supervised learning",
  "transfer learning", "Bayesian optimization").
- model: established model architectures or well-known named models the paper itself uses
  (e.g. "transformer", "CNN", "LSTM", "graph neural network", "YOLO", "ResNet", "LLM", "GPT-4").
  If a term names an architecture or a model, put it here, not in methods.
  Do not include the name of a new model proposed in this paper (e.g. "RSC-Net");
  instead include the established architecture it is built on, if the text names one.
- modality: the types of data the paper itself uses
  (e.g. "video", "broadcast video", "IMU", "EEG", "trajectory", "3D skeleton", "text").

Rules:
- Include only what the paper itself studies or uses. Do not include methods or data mentioned
  only as background, prior work, or baselines for comparison.
- Each term must be a short noun phrase of 1 to 4 words that other papers could also use,
  so that terms can be counted across papers. Do not output verb phrases or sentence fragments.
  You may rephrase the source into such a noun phrase, e.g.
    source "reconstruct textured 3D pedestrians" -> term "3D human reconstruction"
    source "in a weakly-supervised manner"     -> term "weakly-supervised learning"
    source "the probability of qualification"  -> term "qualification probability"
- Split coordinated terms into separate items with the same source, e.g.
    source "3D human pose and shape estimation" -> terms "3D human pose estimation", "3D human shape estimation"
- Keep multi-word terms whole (e.g. "pose estimation", not "pose" and "estimation").
- Write terms in the singular form (e.g. "image", not "images") and lowercase,
  except for acronyms and proper nouns (e.g. "LLM", "NBA", "3D").
- Every term must have a source that actually appears in the title or abstract.
  If you cannot point to such words, do not output the term.
- Do not output generic words such as "method", "model", "approach", "framework", "system",
  "data", "dataset", "performance", "analysis".
- The examples above only illustrate each category. Never output an example term
  unless the title or abstract actually supports it.
- Do not repeat the same term. Return an empty list if nothing applies.

Title: {title}

Abstract: {abstract}
"""


# ===== Gemini API =====
def api_key():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("環境変数 GEMINI_API_KEY が設定されていません")
    return key


def list_models():
    """generateContent に使えるモデル名を一覧表示する"""
    res = requests.get(f"{API_BASE}/models", headers={"x-goog-api-key": api_key()},
                       params={"pageSize": 1000}, timeout=30)
    res.raise_for_status()
    for m in res.json().get("models", []):
        if "generateContent" in m.get("supportedGenerationMethods", []):
            print(m["name"].removeprefix("models/"))


def generate(prompt):
    """プロンプトを送り、JSON（dict）を返す。429や5xxは待って再試行する"""
    url = f"{API_BASE}/models/{MODEL}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,                      # 毎回できるだけ同じ結果にする
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }
    for attempt in range(RETRY + 1):
        try:
            res = requests.post(url, headers={"x-goog-api-key": api_key()}, json=body, timeout=60)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt >= RETRY:
                raise
            wait = 30 * (2 ** attempt)
            print(f"    {type(e).__name__}。{wait}秒後に再試行します")
            time.sleep(wait)
            continue
        if res.status_code == 429 or res.status_code >= 500:
            if attempt >= RETRY:
                res.raise_for_status()
            wait = 30 * (2 ** attempt)
            print(f"    {res.status_code}エラー。{wait}秒後に再試行します")
            time.sleep(wait)
            continue
        if res.status_code != 200:
            sys.exit(f"Gemini APIのエラー {res.status_code}: {res.text[:500]}")
        data = res.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            raise ValueError(f"応答に結果が含まれていません: {json.dumps(data)[:500]}")
        return json.loads(text)
    raise RuntimeError("再試行の上限に達しました")


# ===== 出力の確認 =====
def validate(result):
    """形が {項目: [{"term": str, "source": str}, ...]} かを確認し、同じ term の重複を除いて返す"""
    if not isinstance(result, dict) or set(result) != set(FIELDS):
        raise ValueError(f"キーが想定と違います: {result}")
    cleaned = {}
    for f in FIELDS:
        items = result[f]
        if not isinstance(items, list):
            raise ValueError(f"{f} がリストではありません: {items}")
        seen, out = set(), []
        for item in items:
            if (not isinstance(item, dict) or set(item) != {"term", "source"}
                    or not all(isinstance(v, str) for v in item.values())):
                raise ValueError(f"{f} の要素が {{term, source}} の形ではありません: {item}")
            term, source = item["term"].strip(), item["source"].strip()
            if term and term.lower() not in seen:
                seen.add(term.lower())
                out.append({"term": term, "source": source})
        cleaned[f] = out
    return cleaned


def empty_to_null(result):
    """当てはまる語がない項目（空のリスト）を None（JSONでは null）にする"""
    return {f: (values or None) for f, values in result.items()}


def appears_in(term, text):
    """語がタイトルか要旨に出てくるか（大文字小文字、複数形の s/es、ハイフンと空白の違いは無視）"""
    words = [re.escape(w) for w in re.split(r"[\s\-]+", term.lower()) if w]
    if not words:
        return False
    pattern = r"\b" + r"[\s\-]+".join(words) + r"(?:s|es)?\b"
    return re.search(pattern, text.lower()) is not None


DROP_NOT_FOUND = True   # True: source が本文に見つからない語を result から除く（除いた語は "not_found" に残る）


def check_terms(result, text):
    """各語の source（根拠となる本文の表記）が、タイトルか要旨に出てくるかを確認する。
    出てくる語を kept、出てこない語（捏造やプロンプトの例の写しの可能性がある）を not_found として返す"""
    kept, not_found = {}, {}
    for f in FIELDS:
        ok, nf = [], []
        for item in result[f] or []:
            (ok if appears_in(item["source"], text) else nf).append(item)
        kept[f], not_found[f] = ok or None, nf or None
    return kept, not_found


# ===== メイン =====
def spreadsheet_id_from_gsheet(path):
    """Google ドライブのショートカット（.gsheet）から、スプレッドシートのIDを取り出す。
    .gsheet の中身は {"url": "...", "doc_id": "...", ...} という短いJSONで、データ自体は入っていない"""
    with open(path, encoding="utf-8") as f:
        info = json.load(f)
    if info.get("doc_id"):
        return info["doc_id"]
    m = re.search(r"/spreadsheets/d/([\w-]+)", info.get("url", ""))
    if not m:
        sys.exit(f"{path} からスプレッドシートのIDを取り出せませんでした: {info}")
    return m.group(1)


def read_papers():
    """論文の一覧を読み込む。.gsheet ならスプレッドシートの PAPERS_SHEET シートから、それ以外は CSV から読む"""
    if PAPERS_PATH.endswith(".gsheet"):
        from upload_to_sheets import get_client  # GCP_SA_KEY または GCP_SA_KEY_FILE で認証
        sheet_id = spreadsheet_id_from_gsheet(PAPERS_PATH)
        print(f"スプレッドシート {sheet_id} の「{PAPERS_SHEET}」シートを読み込みます")
        ws = get_client().open_by_key(sheet_id).worksheet(PAPERS_SHEET)
        return ws.get_all_records(numericise_ignore=["all"])  # 論文IDを数値に変換しない
    with open(PAPERS_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_papers(ids):
    """論文IDの指定があればその論文を、なければランダムに N_PAPERS 件を返す。
    papers に見つからない論文IDは {"paper_id": ID} だけを返す（結果を null にするため）"""
    papers = read_papers()
    if ids:
        by_id = {str(p["paper_id"]): p for p in papers}
        return [by_id.get(i, {"paper_id": i}) for i in ids]
    return random.Random(SEED).sample(papers, min(N_PAPERS, len(papers)))


def main():
    args = sys.argv[1:]
    if args == ["--list-models"]:
        list_models()
        return

    papers = load_papers(args or PAPER_IDS)
    print(f"モデル: {MODEL} / 論文: {len(papers)}件\n")
    outputs = []
    called = False
    for p in papers:
        if "title" not in p:  # papers に見つからない論文ID
            print(f"[{p['paper_id']}] papers に見つからないため null にします\n")
            outputs.append({"paper_id": p["paper_id"], "title": None,
                            "result": None, "not_found": None})
            continue
        if called:
            time.sleep(WAIT_SEC)
        called = True
        prompt = PROMPT.format(title=p["title"], abstract=p["abstract"])
        result = empty_to_null(validate(generate(prompt)))
        kept, missing = check_terms(result, p["title"] + " " + p["abstract"])

        print(f"[{p['paper_id']}] {p['title']}")
        for f in FIELDS:
            items = result[f] or []
            if not items:
                print(f"  {f:9}: null")
                continue
            for k, item in enumerate(items):
                label = f"{f:9}:" if k == 0 else " " * 10
                mark = " (本文に見つからない)" if item in (missing[f] or []) else ""
                print(f"  {label} {item['term']}  <- \"{item['source']}\"{mark}")
        print()
        outputs.append({"paper_id": p["paper_id"], "title": p["title"],
                        "result": kept if DROP_NOT_FOUND else result,
                        "not_found": missing})

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"model": MODEL, "papers": outputs}, f, ensure_ascii=False, indent=2)
    print(f"{OUTPUT_JSON} に保存しました")


if __name__ == "__main__":
    main()
