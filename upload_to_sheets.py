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

def upload(sh, csv_path, sheet_title):
    values = load_csv(csv_path)
    header, rows = values[0], values[1:]
    ws = get_or_create_worksheet(sh, sheet_title)

    # 1行目が空ならヘッダーを書き込む（get_all_values()は空でも[[]]を返すことがあるため使わない）
    if not ws.row_values(1):
        ws.update(values=[header], range_name="A1", value_input_option="RAW")
        print(f'ヘッダーが空だったので書き込みます')

    # 1列目（paper_id）にある論文IDを取得する（1行目はヘッダーなので除く）
    existing_ids = set(ws.col_values(1)[1:])

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