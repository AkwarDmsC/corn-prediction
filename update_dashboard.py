#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
面板数据更新器 v1.0
从 predictions.md 提取最新数据写入 dashboard_data.json
由 cron 在每次分析后自动调用
"""


import re, json
from pathlib import Path

from constants import PREDICTIONS, DASHBOARD_DATA

PRED_FILE = PREDICTIONS
OUTPUT_FILE = DASHBOARD_DATA

def update():
    if not PRED_FILE.exists():
        print("[dashboard] predictions.md not found")
        return False

    content = PRED_FILE.read_text()
    blocks = re.split(r'\n(?=## 预测)', content)
    latest_block = blocks[1] if len(blocks) > 1 else blocks[0]

    d = {}
    def g(pat, t, g=1):
        m = re.search(pat, t)
        return m.group(g).strip() if m else None

    d['date'] = g(r'## 预测 (\d{4}-\d{2}-\d{2})', latest_block)
    d['day_close'] = g(r'\*\*日盘收盘\(15:00\)\*\*:\s*([\d.]+)', latest_block)
    d['night_close'] = g(r'\*\*夜盘收盘\(23:00\)\*\*:\s*([\d.]+)', latest_block)

    # 日盘 section
    idx = latest_block.find('### 🌞 日盘')
    if idx >= 0:
        seg = latest_block[idx:]
        end = seg.find('### ', 10)
        if end < 0: end = len(seg)
        ds = seg[:end]
        d['day_pred_base'] = g(r'预测基准\s*\|\s*([\d.]+)', ds)
        d['day_dir'] = g(r'方向\s*\|\s*(偏强|偏弱|震荡)', ds)
        d['day_ml_low'] = g(r'ML预测低价\s*\|\s*([\d.]+)', ds)
        d['day_ml_high'] = g(r'ML预测高价\s*\|\s*([\d.]+)', ds)
        d['day_conf'] = g(r'置信度\s*\|\s*(高|中|低).*', ds)
        rng = re.search(r'预测区间\s*\|\s*([\d.]+) ~ ([\d.]+)', ds)
        if rng:
            d['day_range_lo'] = rng.group(1)
            d['day_range_hi'] = rng.group(2)

    # 信号快照
    idx2 = latest_block.find('### 信号快照')
    if idx2 >= 0:
        seg2 = latest_block[idx2:]
        end2 = seg2.find('### ', 10)
        if end2 < 0: end2 = len(seg2)
        ss = seg2[:end2]
        d['signals'] = {
            'rsi': g(r'RSI\(14\)\s*\|\s*([\d.]+)', ss),
            'vol_ratio': g(r'成交量比率\s*\|\s*([\d.]+)x', ss),
            'bb_position': g(r'布林带位置\s*\|\s*([\d.]+)%', ss),
            'season': g(r'季节性\s*\|\s*([^|]+)', ss),
        }
        ma_m = re.search(r'MA5/MA10/MA20\s*\|\s*([\d.]+)/([\d.]+)/([\d.]+)', ss)
        if ma_m:
            d['signals']['ma'] = list(ma_m.groups())

    # 短中长期
    idx3 = latest_block.find('### 📆 短中长期预测')
    if idx3 >= 0:
        seg3 = latest_block[idx3:]
        end3 = seg3.find('### ', 10)
        if end3 < 0: end3 = len(seg3)
        st = seg3[:end3]
        st_m = re.search(r'\|\s*短期[^|]*\|\s*[^|]*\|\s*([\d.]+)~([\d.]+)', st)
        if st_m: d['st'] = st_m.group(1)+'~'+st_m.group(2)
        mt_m = re.search(r'\|\s*中期[^|]*\|\s*[^|]*\|\s*([\d.]+)~([\d.]+)', st)
        if mt_m: d['mt'] = mt_m.group(1)+'~'+mt_m.group(2)
        lt_m = re.search(r'\|\s*长期[^|]*\|\s*[^|]*\|\s*([\d.]+)~([\d.]+)', st)
        if lt_m: d['lt'] = lt_m.group(1)+'~'+lt_m.group(2)

    d['total_blocks'] = len(blocks) - 1
    d['verified_day'] = len(re.findall(r'\|\s*🌞 日盘\s*\|', content))
    d['verified_night'] = len(re.findall(r'\|\s*🌙 夜盘\s*\|', content))

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(f"[dashboard] 面板数据已更新: {d.get('date','?')} | {OUTPUT_FILE}")
    return True

if __name__ == '__main__':
    update()

