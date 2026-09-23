# -*- coding: utf-8 -*-
"""
진단용 스크립트. KRX 시총 데이터가 왜 비어있는지 확인한다.
실행: python diag_marcap.py
"""
import FinanceDataReader as fdr

df = fdr.StockListing("KRX-MARCAP")
print("컬럼 목록:", list(df.columns))
print("행 개수:", len(df))
print()
print("상위 5행:")
print(df.head())
print()

for c in df.columns:
    if c.lower() in ("marcap", "markcap", "marketcap"):
        col = df[c]
        print(f"'{c}' 컬럼 — 결측 개수: {col.isna().sum()} / {len(col)}, dtype: {col.dtype}")
        print("샘플 값 5개:", col.head(5).tolist())

input("\n=== 엔터를 누르면 창이 닫힙니다 ===")
