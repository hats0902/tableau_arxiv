"""
CSV（papers / keywords / paper_categories）を Google スプレッドシートの各シートに書き込む
初回だけ実行するファイル

必要な環境変数:
    GCP_SA_KEY     : サービスアカウントの認証キー（JSONファイルの中身そのもの）
    SPREADSHEET_ID : スプレッドシートのID（URLの /d/ と /edit の間の文字列）

ローカルで試すときは、JSONファイルのパスを GCP_SA_KEY_FILE に指定しても動く
"""
import csv
import json
import os

import gspread

# CSVファイル → 書き込み先のシート名
TARGETS = {
    "papers.csv": "papers",
    "keywords.csv": "keywords",
    "paper_categories.csv": "paper_categories",
}

# 数値・真偽値として書き込む列（それ以外は文字列のまま）
INT_COLUMNS = {"year", "count"}
BOOL_COLUMNS = {"is_primary"}

CHUNK_ROWS = 2000  # 1回のリクエストで書き込む行数（大きすぎるとAPIの上限に当たる）


def get_client():
    """GitHub Actionsでは環境変数のJSON、ローカルではJSONファイルで認証する"""
    if os.environ.get("GCP_SA_KEY"):
        return gspread.service_account_from_dict(json.loads(os.environ["GCP_SA_KEY"]))
    return gspread.service_account(filename=os.environ["GCP_SA_KEY_FILE"])


def load_csv(path):
    """CSVを読み込み、[ヘッダー, 行, 行, ...] の形で返す。数値・真偽値の列は型を変換する"""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = [header]
        for row in reader:
            converted = []
            for col, value in zip(header, row):
                if col in INT_COLUMNS and value != "":
                    converted.append(int(value))
                elif col in BOOL_COLUMNS:
                    converted.append(value == "True")
                else:
                    converted.append(value)
            rows.append(converted)
    return rows


def get_or_create_worksheet(sh, title):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=1, cols=1)

# # 全件書き換え方式
# def upload(sh, csv_path, sheet_title):
#     values = load_csv(csv_path)
#     ws = get_or_create_worksheet(sh, sheet_title)

#     # 毎回シートを空にしてから全件を書き直す（CSVが常に正しい元データという考え方）
#     ws.clear()
#     ws.resize(rows=len(values), cols=len(values[0]))

#     for start in range(0, len(values), CHUNK_ROWS):
#         chunk = values[start:start + CHUNK_ROWS]
#         # RAW: "2101.00010" のような論文IDが数値に変換されて末尾の0が消えるのを防ぐ
#         ws.update(values=chunk, range_name=f"A{start + 1}", value_input_option="RAW")

#     print(f"{csv_path} → シート「{sheet_title}」: {len(values) - 1}行を書き込みました")
    
# 付け足す方式
def upload(sh, csv_path, sheet_title):
    values = load_csv(csv_path)
    header, rows = values[0], values[1:]
    ws = get_or_create_worksheet(sh, sheet_title)

    existing = ws.get_all_values()
    if not existing:
        # シートが空なら、ヘッダーから書き込む
        ws.append_rows([header], value_input_option="RAW")
        existing_ids = set()
    else:
        # 1列目（paper_id）にある論文IDを取得する
        existing_ids = {r[0] for r in existing[1:]}

    # シートにまだない論文の行だけを追加する（二重追加を防ぐ）
    new_rows = [r for r in rows if r[0] not in existing_ids]
    for start in range(0, len(new_rows), CHUNK_ROWS):
        ws.append_rows(new_rows[start:start + CHUNK_ROWS], value_input_option="RAW")

    print(f"{csv_path} → シート「{sheet_title}」: {len(new_rows)}行を追加しました")


def main():
    gc = get_client()
    sh = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    for csv_path, sheet_title in TARGETS.items():
        if not os.path.exists(csv_path):
            print(f"{csv_path} が見つからないためスキップします")
            continue
        upload(sh, csv_path, sheet_title)


if __name__ == "__main__":
    main()