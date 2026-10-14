import csv
import os
from datetime import datetime, timedelta, timezone
 
from arxiv_api_title_withwiki import (
    CATEGORIES, KEYWORDS,
    PAPERS_CSV, KEYWORDS_CSV, CATEGORIES_CSV,
    PAPER_FIELDS, KEYWORD_FIELDS, CATEGORY_FIELDS,
    build_query, fetch_month, extract_keyword_rows, category_rows,
)
from upload_to_sheets import get_client, get_or_create_worksheet, CHUNK_ROWS
 
DAYS_BACK = 3  # 直近何日分を取得するか（週1回の実行に対して2週間分。1回失敗しても取りこぼさない）
 
# シート名 → (CSVファイル, 列の並び)
SHEETS = {
    "papers": (PAPERS_CSV, PAPER_FIELDS),
    "keywords": (KEYWORDS_CSV, KEYWORD_FIELDS),
    "paper_categories": (CATEGORIES_CSV, CATEGORY_FIELDS),
}
 
 
def existing_ids(ws):
    """シートの1列目（paper_id）にある論文IDを取得する。1行目はヘッダーなので除く"""
    return set(ws.col_values(1)[1:])
 
 
def append_new_rows(ws, rows, fields):
    """シートにまだない論文の行だけを追記する（シートごとに判定するので途中で失敗しても重複しない）"""
    if not ws.row_values(1):
        ws.append_rows([fields], value_input_option="RAW")  # 空のシートならヘッダーを書く
    known = existing_ids(ws)
    new_rows = [[r[f] for f in fields] for r in rows if r["paper_id"] not in known]
    for start in range(0, len(new_rows), CHUNK_ROWS):
        # RAW: "2101.00010" のような論文IDが数値に変換されないようにする
        ws.append_rows(new_rows[start:start + CHUNK_ROWS], value_input_option="RAW")
    return len(new_rows)
 
 
def export_to_csv(ws, csv_path):
    """シートの内容でCSVを上書きする"""
    values = ws.get_all_values()
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(values)
    return len(values) - 1
 
 
def main():
    # ----- 1. 直近の論文を取得 -----
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=DAYS_BACK)
    query = build_query(CATEGORIES, KEYWORDS, start, end)
    print(f"取得期間: {start:%Y-%m-%d} 〜 {end:%Y-%m-%d} (UTC)")
    papers, _ = fetch_month(query, None)
 
    gc = get_client()
    sh = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    ws = {name: get_or_create_worksheet(sh, name) for name in SHEETS}
 
    # ----- 2. 新しい論文だけをシートに追記 -----
    known = existing_ids(ws["papers"])
    new_papers = [p for p in papers if p["paper_id"] not in known]
    print(f"取得 {len(papers)}件 / うちシートにない論文 {len(new_papers)}件")
 
    if new_papers:
        rows_by_sheet = {
            "keywords": extract_keyword_rows(new_papers),
            "paper_categories": category_rows(new_papers),
            "papers": new_papers,
        }
        # papers を最後に追記する。途中で失敗しても、次回は papers に載っていない論文として
        # 取り直され、keywords などは各シートの論文IDで重複が防がれる
        for name in ["keywords", "paper_categories", "papers"]:
            n = append_new_rows(ws[name], rows_by_sheet[name], SHEETS[name][1])
            print(f"  シート「{name}」に {n}行を追記")
 
    # ----- 3. シートの内容でCSVを更新 -----
    for name, (csv_path, _) in SHEETS.items():
        n = export_to_csv(ws[name], csv_path)
        print(f"シート「{name}」→ {csv_path}: {n}行")
 
 
if __name__ == "__main__":
    main()