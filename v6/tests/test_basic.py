"""
v6 模块基础测试
用法: python3 -m pytest tests/test_basic.py -v
      或 python3 tests/test_basic.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np


def _sample_df(n_days: int = 42) -> pd.DataFrame:
    """生成模拟 DCE 日线数据"""
    np.random.seed(42)
    dates = pd.date_range('2026-04-01', periods=n_days, freq='B')
    close = 2400 + np.cumsum(np.random.randn(n_days) * 8)
    return pd.DataFrame({
        'date': dates,
        'open': close - np.random.uniform(0, 5, n_days),
        'high': close + np.random.uniform(5, 15, n_days),
        'low': close - np.random.uniform(5, 15, n_days),
        'close': close,
        'volume': np.random.randint(100000, 500000, n_days),
    })


def test_imports():
    from config import CORE_WEIGHTS, VERSION
    from schemas import SignalSnapshot, Indicators, Scenario, PredictionResult
    from signals import compute_signal_snapshot, prepare_corn_df, sma, rsi
    from predictor import analyze_corn_v6, predict_hl_v6, predict_night_v6
    from formatter import format_v51_output, format_json_output
    print(f'  Version={VERSION}, Weights={len(CORE_WEIGHTS)}')


def test_signal_snapshot():
    from signals import compute_signal_snapshot
    df = _sample_df()
    sig = compute_signal_snapshot(df, cbot_chg=-0.3, weather_score=-0.2)
    assert 'confidence' in sig
    assert 'indicators' in sig
    assert 'signals' in sig
    assert 'effective_signals' in sig
    assert sig['version'] == 'v6.0'
    assert len(sig['signals']) == 9
    print(f'  conf={sig["confidence"]}, cons={sig["filtered_consistency"]:.3f}')


def test_full_pipeline():
    from predictor import analyze_corn_v6
    df = _sample_df()
    result = analyze_corn_v6(df, cbot_chg=-0.3, run_ml=False)
    assert 'day' in result
    assert 'night' in result
    assert 'signal' in result
    assert 'full_day_range' in result
    print(f'  day.dir={result["day"]["direction"]}, night.dir={result["night"]["direction"]}')


def test_formatter():
    from predictor import analyze_corn_v6
    from formatter import format_v51_output, format_json_output
    df = _sample_df()
    result = analyze_corn_v6(df, cbot_chg=-0.3, run_ml=False)
    text = format_v51_output(result)
    assert len(text) > 100
    assert '日盘' in text
    assert '夜盘' in text

    j = format_json_output(result)
    assert isinstance(j, dict)
    assert 'day' in j
    assert 'night' in j
    assert 'signals' in j
    print(f'  text={len(text)}c, JSON keys={sorted(j.keys())}')


def test_model_paths():
    from config import MODEL_HIGH, MODEL_LOW, MODEL_NIGHT, MODELS_DIR
    assert MODELS_DIR.exists()
    assert MODEL_HIGH.exists()
    assert MODEL_LOW.exists()
    assert MODEL_NIGHT.exists()
    assert MODEL_HIGH.stat().st_size > 100000  # ~380K
    print(f'  models: 3 .pkl files in {MODELS_DIR.name}')


def test_ml_prediction():
    from config import MODEL_HIGH, MODEL_LOW, MODEL_NIGHT
    from predictor import predict_hl_v6, predict_night_v6
    df = _sample_df(60)
    hl = predict_hl_v6(df)
    assert 'pred_high' in hl
    assert 'pred_low' in hl
    assert hl['pred_high'] > hl['pred_low']
    assert hl['range'] > 0
    print(f'  HL: high={hl["pred_high"]:.0f}, low={hl["pred_low"]:.0f}')

    price = float(df['close'].iloc[-1])
    night = predict_night_v6(df, price, cbot_chg=0.5)
    assert 'pred' in night
    assert 'direction' in night
    assert night['ridge_pred'] is not None
    print(f'  Night: pred={night["pred"]:.0f}, dir={night["direction"]}')


if __name__ == '__main__':
    print('v6 Phase 1 smoke tests')
    failed = []
    for name in ['test_imports', 'test_signal_snapshot', 'test_full_pipeline',
                 'test_formatter', 'test_model_paths', 'test_ml_prediction']:
        try:
            globals()[name]()
            print(f'  ✅ {name}')
        except Exception as e:
            print(f'  ❌ {name}: {e}')
            import traceback
            traceback.print_exc()
            failed.append(name)
    if failed:
        print(f'FAILED: {failed}')
        sys.exit(1)
    print('All passed ✅')
